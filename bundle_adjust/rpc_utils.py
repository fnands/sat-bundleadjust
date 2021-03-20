# Copyright (C) 2015, Carlo de Franchis <carlo.de-franchis@cmla.ens-cachan.fr>
# Copyright (C) 2015, Gabriele Facciolo <facciolo@cmla.ens-cachan.fr>
# Copyright (C) 2015, Enric Meinhardt <enric.meinhardt@cmla.ens-cachan.fr>


from __future__ import print_function

import json
import warnings

import bs4
import numpy as np
import pyproj
import rasterio
import rpcm
from rasterio.errors import NotGeoreferencedWarning
from s2p import common, geographiclib
from s2p.config import cfg

warnings.filterwarnings("ignore", category=NotGeoreferencedWarning)

import matplotlib.pyplot as plt


def approx_rpc_as_proj_matrix_opencv(rpc_model, col_range, lin_range, alt_range, img_size, verbose=True):
    """
    Returns a least-square approximation of the RPC functions as a projection
    matrix. The approximation is optimized on a sampling of the 3D region
    defined by the altitudes in alt_range and the image tile defined by
    col_range and lin_range.
    """

    # https://docs.opencv.org/2.4/modules/calib3d/doc/camera_calibration_and_3d_reconstruction.html

    ### step 1: generate cartesian coordinates of 3d points used to fit the
    ###         best projection matrix
    # get mesh points and convert them to geodetic then to geocentric
    # coordinates
    cols, lins, alts = generate_point_mesh(col_range, lin_range, alt_range)
    lons, lats = rpc_model.localization(cols, lins, alts)
    x, y, z = geographiclib.lonlat_to_geocentric(lons, lats, alts)

    ### step 2: estimate the camera projection matrix from corresponding
    # 3-space and image entities
    world_points = np.vstack([x, y, z]).T
    image_points = np.vstack([cols, lins]).T
    import cv2

    camera_matrix = cv2.initCameraMatrix2D(
        [world_points.astype(np.float32)], [image_points.astype(np.float32)], img_size
    )
    calibration_flags = (
        cv2.CALIB_USE_INTRINSIC_GUESS
        + cv2.CALIB_FIX_K1
        + cv2.CALIB_FIX_K2
        + cv2.CALIB_FIX_K3
        + cv2.CALIB_FIX_K4
        + cv2.CALIB_FIX_K5
        + cv2.CALIB_FIX_K6
        + cv2.CALIB_ZERO_TANGENT_DIST
    )
    ret, mtx, dist, rvecs, tvecs = cv2.calibrateCamera(
        [world_points.astype(np.float32)],
        [image_points.astype(np.float32)],
        img_size,
        camera_matrix,
        None,
        flags=calibration_flags,
    )
    print("ret", ret)
    R, _ = cv2.Rodrigues(rvecs[0])
    P = mtx @ np.hstack((R, tvecs[0]))

    print("intrinsic parameters: {}".format(mtx))
    print("extrinsic parameters: \nR:{},\nT:{}".format(rvecs, tvecs))
    print("distortion parameters: {}".format(dist))

    ### step 3: for debug, test the approximation error
    # compute the projection error made by the computed matrix P, on the
    # used learning points
    proj = P @ np.hstack((world_points, np.ones((world_points.shape[0], 1)))).T
    image_pointsPROJ = (proj[:2, :] / proj[-1, :]).T
    colPROJ, linPROJ = image_pointsPROJ[:, 0], image_pointsPROJ[:, 1]
    d_col, d_lin = cols - colPROJ, lins - linPROJ
    mean_err = np.mean(np.linalg.norm(image_points - image_pointsPROJ, axis=1))

    if verbose:
        _, f = plt.subplots(1, 2, figsize=(10, 3))
        f[0].hist(np.sort(d_col), bins=40)
        f[0].title.set_text("col diffs")
        f[1].hist(np.sort(d_lin), bins=40)
        f[1].title.set_text("row diffs")
        plt.show()

        print("approximate_rpc_as_projective: (min, max, mean)")
        print("distance on cols:", np.min(d_col), np.max(d_col), np.mean(d_col))
        print("distance on rows:", np.min(d_lin), np.max(d_lin), np.mean(d_lin))

    return P, mean_err


def approx_rpc_as_proj_matrix(rpc_model, col_range, lin_range, alt_range, verbose=False):
    """
    Returns a least-square approximation of the RPC functions as a projection
    matrix. The approximation is optimized on a sampling of the 3D region
    defined by the altitudes in alt_range and the image tile defined by
    col_range and lin_range.
    """
    ### step 1: generate cartesian coordinates of 3d points used to fit the
    ###         best projection matrix
    # get mesh points and convert them to geodetic then to geocentric
    # coordinates
    cols, lins, alts = generate_point_mesh(col_range, lin_range, alt_range)
    lons, lats = rpc_model.localization(cols, lins, alts)
    x, y, z = geographiclib.lonlat_to_geocentric(lons, lats, alts)

    ### step 2: estimate the camera projection matrix from corresponding
    # 3-space and image entities
    world_points = np.vstack([x, y, z]).T
    image_points = np.vstack([cols, lins]).T
    P = camera_matrix(world_points, image_points)

    ### step 3: for debug, test the approximation error
    # compute the projection error made by the computed matrix P, on the
    # used learning points
    proj = P @ np.hstack((world_points, np.ones((world_points.shape[0], 1)))).T
    image_pointsPROJ = (proj[:2, :] / proj[-1, :]).T
    colPROJ, linPROJ = image_pointsPROJ[:, 0], image_pointsPROJ[:, 1]
    d_col, d_lin = cols - colPROJ, lins - linPROJ
    mean_err = np.mean(np.linalg.norm(image_points - image_pointsPROJ, axis=1))

    if verbose:
        _, f = plt.subplots(1, 2, figsize=(10, 3))
        f[0].hist(np.sort(d_col), bins=40)
        f[0].title.set_text("col diffs")
        f[1].hist(np.sort(d_lin), bins=40)
        f[1].title.set_text("row diffs")
        plt.show()

        print("approximate_rpc_as_projective: (min, max, mean)")
        print("distance on cols:", np.min(d_col), np.max(d_col), np.mean(d_col))
        print("distance on rows:", np.min(d_lin), np.max(d_lin), np.mean(d_lin))

    return P, mean_err


def find_corresponding_point(model_a, model_b, x, y, z):
    """
    Finds corresponding points in the second image, given the heights.

    Arguments:
        model_a, model_b: two instances of the rpcm.RPCModel class, or of
            the projective_model.ProjModel class
        x, y, z: three 1D numpy arrrays, of the same length. x, y are the
        coordinates of pixels in the image, and z contains the altitudes of the
        corresponding 3D point.

    Returns:
        xp, yp, z: three 1D numpy arrrays, of the same length as the input. xp,
            yp contains the coordinates of the projection of the 3D point in image
            b.
    """
    t1, t2 = model_a.localization(x, y, z)
    xp, yp = model_b.projection(t1, t2, z)
    return (xp, yp, z)


def compute_height(model_a, model_b, x1, y1, x2, y2):
    """
    Computes the height of a point given its location inside two images.

    Arguments:
        model_a, model_b: two instances of the rpcm.RPCModel class, or of
            the projective_model.ProjModel class
        x1, y1: two 1D numpy arrrays, of the same length, containing the
            coordinates of points in the first image.
        x2, y2: two 2D numpy arrrays, of the same length, containing the
            coordinates of points in the second image.

    Returns:
        a 1D numpy array containing the list of computed heights.
    """
    n = len(x1)
    h0 = np.zeros(n)
    h0_inc = h0
    p2 = np.vstack([x2, y2]).T
    HSTEP = 1
    err = np.zeros(n)

    for i in range(100):
        tx, ty, tz = find_corresponding_point(model_a, model_b, x1, y1, h0)
        r0 = np.vstack([tx, ty]).T
        tx, ty, tz = find_corresponding_point(model_a, model_b, x1, y1, h0 + HSTEP)
        r1 = np.vstack([tx, ty]).T
        a = r1 - r0
        b = p2 - r0
        # implements: h0_inc = dot(a,b) / dot(a,a)
        # For some reason, the formulation below causes massive memory leaks on
        # some systems.
        # h0_inc = np.divide(np.diag(np.dot(a, b.T)), np.diag(np.dot(a, a.T)))
        # Replacing with the equivalent:
        diagabdot = np.multiply(a[:, 0], b[:, 0]) + np.multiply(a[:, 1], b[:, 1])
        diagaadot = np.multiply(a[:, 0], a[:, 0]) + np.multiply(a[:, 1], a[:, 1])
        h0_inc = np.divide(diagabdot, diagaadot)
        #        if np.any(np.isnan(h0_inc)):
        #            print(x1, y1, x2, y2)
        #            print(a)
        #            return h0, h0*0
        # implements:   q = r0 + h0_inc * a
        q = r0 + np.dot(np.diag(h0_inc), a)
        # implements: err = sqrt(dot(q-p2, q-p2))
        tmp = q - p2
        err = np.sqrt(np.multiply(tmp[:, 0], tmp[:, 0]) + np.multiply(tmp[:, 1], tmp[:, 1]))
        #       print(np.arctan2(tmp[:, 1], tmp[:, 0])) # for debug
        #       print(err) # for debug
        h0 = np.add(h0, h0_inc * HSTEP)
        # implements: if fabs(h0_inc) < 0.0001:
        if np.max(np.fabs(h0_inc)) < 0.001:
            break

    return h0, err


def geodesic_bounding_box(rpc, x, y, w, h):
    """
    Computes a bounding box on the WGS84 ellipsoid associated to a Pleiades
    image region of interest, through its rpc function.

    Args:
        rpc: instance of the rpcm.RPCModel class
        x, y, w, h: four integers defining a rectangular region of interest
            (ROI) in the image. (x, y) is the top-left corner, and (w, h) are
            the dimensions of the rectangle.

    Returns:
        4 geodesic coordinates: the min and max longitudes, and the min and
        max latitudes.
    """
    # compute altitude coarse extrema from rpc data
    m = rpc.alt_offset - rpc.alt_scale
    M = rpc.alt_offset + rpc.alt_scale

    # build an array with vertices of the 3D ROI, obtained as {2D ROI} x [m, M]
    x = np.array([x, x, x, x, x + w, x + w, x + w, x + w])
    y = np.array([y, y, y + h, y + h, y, y, y + h, y + h])
    a = np.array([m, M, m, M, m, M, m, M])

    # compute geodetic coordinates of corresponding world points
    lon, lat = rpc.localization(x, y, a)

    # extract extrema
    # TODO: handle the case where longitudes pass over -180 degrees
    # for latitudes it doesn't matter since for latitudes out of [-60, 60]
    # there is no SRTM data
    return np.min(lon), np.max(lon), np.min(lat), np.max(lat)


def altitude_range_coarse(rpc, scale_factor=1):
    """
    Computes a coarse altitude range using the RPC informations only.

    Args:
        rpc: instance of the rpcm.RPCModel class
        scale_factor: factor by which the scale offset is multiplied

    Returns:
        the altitude validity range of the RPC.
    """
    m = rpc.alt_offset - scale_factor * rpc.alt_scale
    M = rpc.alt_offset + scale_factor * rpc.alt_scale
    return m, M


def min_max_heights_from_bbx(im, lon_m, lon_M, lat_m, lat_M, rpc):
    """
    Compute min, max heights from bounding box

    Args:
        im: path to an image file
        lon_m, lon_M, lat_m, lat_M: bounding box

    Returns:
        hmin, hmax: min, max heights
    """
    # open image
    dataset = rasterio.open(im, "r")

    # convert lon/lat to im projection
    epsg = "epsg:4326"
    x_im_proj, y_im_proj = pyproj.transform(
        pyproj.Proj(init=epsg), pyproj.Proj(init=dataset.crs["init"]), [lon_m, lon_M], [lat_m, lat_M]
    )

    # convert im projection to pixel
    pts = []
    pts.append(~dataset.transform * (x_im_proj[0], y_im_proj[0]))
    pts.append(~dataset.transform * (x_im_proj[1], y_im_proj[1]))
    px = [p[0] for p in pts]
    py = [p[1] for p in pts]

    # get footprint
    [px_min, px_max, py_min, py_max] = map(int, [np.amin(px), np.amax(px) + 1, np.amin(py), np.amax(py) + 1])

    # limits of im extract
    x, y, w, h = px_min, py_min, px_max - px_min + 1, py_max - py_min + 1
    sizey, sizex = dataset.shape
    x0 = np.clip(x, 0, sizex - 1)
    y0 = np.clip(y, 0, sizey - 1)
    w -= x0 - x
    h -= y0 - y
    w = np.clip(w, 0, sizex - 1 - x0)
    h = np.clip(h, 0, sizey - 1 - y0)

    # get value for each pixel
    if (w != 0) and (h != 0):
        array = dataset.read(1, window=((y0, y0 + h), (x0, x0 + w))).astype(float)
        array[array == -32768] = np.nan
        hmin = np.nanmin(array)
        hmax = np.nanmax(array)

        if cfg["exogenous_dem_geoid_mode"] is True:
            geoid = geographiclib.geoid_above_ellipsoid((lat_m + lat_M) / 2, (lon_m + lon_M) / 2)
            hmin += geoid
            hmax += geoid
        return hmin, hmax
    else:
        print("WARNING: rpc_utils.min_max_heights_from_bbx: access window out of range")
        print("returning coarse range from rpc")
        return altitude_range_coarse(rpc, cfg["rpc_alt_range_scale_factor"])


def altitude_range(rpc, x, y, w, h, margin_top=0, margin_bottom=0):
    """
    Computes an altitude range using the exogenous dem.

    Args:
        rpc: instance of the rpcm.RPCModel class
        x, y, w, h: four integers defining a rectangular region of interest
            (ROI) in the image. (x, y) is the top-left corner, and (w, h) are the
            dimensions of the rectangle.
        margin_top: margin (in meters) to add to the upper bound of the range
        margin_bottom: margin (usually negative) to add to the lower bound of
            the range

    Returns:
        lower and upper bounds on the altitude of the world points that are
        imaged by the RPC projection function in the provided ROI. To compute
        these bounds, we use exogenous data. The altitudes are computed with respect
        to the WGS84 reference ellipsoid.
    """
    # TODO: iterate the procedure used here to get a finer estimation of the
    # bounding box on the ellipsoid and thus of the altitude range. For flat
    # regions it will not improve much, but for mountainous regions there is a
    # lot to improve.

    # find bounding box on the ellipsoid (in geodesic coordinates)
    lon_m, lon_M, lat_m, lat_M = geodesic_bounding_box(rpc, x, y, w, h)

    # compute heights on this bounding box
    if cfg["exogenous_dem"] is not None:
        h_m, h_M = min_max_heights_from_bbx(cfg["exogenous_dem"], lon_m, lon_M, lat_m, lat_M, rpc)
        h_m += margin_bottom
        h_M += margin_top
    else:
        print("WARNING: returning coarse range from rpc")
        h_m, h_M = altitude_range_coarse(rpc, cfg["rpc_alt_range_scale_factor"])

    return h_m, h_M


def utm_zone(rpc, x, y, w, h):
    """
    Compute the UTM zone where the ROI probably falls (or close to its border).

    Args:
        rpc: instance of the rpcm.RPCModel class, or path to a GeoTIFF file
        x, y, w, h: four integers defining a rectangular region of interest
            (ROI) in the image. (x, y) is the top-left corner, and (w, h)
            are the dimensions of the rectangle.

    Returns:
        a string of the form '18N' or '18S' where 18 is the utm zone
        identificator.
    """
    # read rpc file
    if not isinstance(rpc, rpcm.RPCModel):
        rpc = rpc_from_geotiff(rpc)

    # determine lat lon of the center of the roi, assuming median altitude
    lon, lat = rpc.localization(x + 0.5 * w, y + 0.5 * h, rpc.alt_offset)[:2]

    return geographiclib.compute_utm_zone(lon, lat)


def utm_roi_to_img_roi(rpc, roi):

    # define utm rectangular box
    x, y, w, h = [roi[k] for k in ["x", "y", "w", "h"]]
    box = [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]

    # convert utm to lon/lat
    utm_proj = geographiclib.utm_proj("{}{}".format(roi["utm_band"], roi["hemisphere"]))
    epsg = "epsg:4326"
    box_lon, box_lat = pyproj.transform(utm_proj, pyproj.Proj(init=epsg), [p[0] for p in box], [p[1] for p in box])

    # project lon/lat vertices into the image
    if not isinstance(rpc, rpcm.RPCModel):
        rpc = rpcm.RPCModel(rpc)
    img_pts = rpc.projection(box_lon, box_lat, rpc.alt_offset)

    # return image roi
    x, y, w, h = common.bounding_box2D(img_pts)
    return {"x": x, "y": y, "w": w, "h": h}


def kml_roi_process(rpc, kml, utm_zone=None):
    """
    Define a rectangular bounding box in image coordinates
    from a polygon in a KML file

    Args:
        rpc: instance of the rpcm.RPCModel class, or path to the xml file
        kml: file path to a KML file containing a single polygon
        utm_zone: force the zone number to be used when defining `utm_bbx`.
            If not specified, the default UTM zone for the given geography
            is used.

    Returns:
        x, y, w, h: four integers defining a rectangular region of interest
            (ROI) in the image. (x, y) is the top-left corner, and (w, h)
            are the dimensions of the rectangle.
    """
    # extract lon lat from kml
    with open(kml, "r") as f:
        a = bs4.BeautifulSoup(f, "lxml").find_all("coordinates")[0].text.split()
    ll_poly = np.array([list(map(float, x.split(","))) for x in a])[:, :2]
    box_d = roi_process(rpc, ll_poly, utm_zone=utm_zone)
    return box_d


def geojson_roi_process(rpc, geojson, utm_zone=None):
    """
    Define a rectangular bounding box in image coordinates
    from a polygon in a geojson file or dict

    Args:
        rpc: instance of the rpcm.RPCModel class, or path to the xml file
        geojson: file path to a geojson file containing a single polygon,
            or content of the file as a dict.
            The geojson's top-level type should be either FeatureCollection,
            Feature, or Polygon.
        utm_zone: force the zone number to be used when defining `utm_bbx`.
            If not specified, the default UTM zone for the given geography
            is used.

    Returns:
        x, y, w, h: four integers defining a rectangular region of interest
            (ROI) in the image. (x, y) is the top-left corner, and (w, h)
            are the dimensions of the rectangle.
    """
    # extract lon lat from geojson file or dict
    if isinstance(geojson, str):
        with open(geojson, "r") as f:
            a = json.load(f)
    else:
        a = geojson

    if a["type"] == "FeatureCollection":
        a = a["features"][0]

    if a["type"] == "Feature":
        a = a["geometry"]

    ll_poly = np.array(a["coordinates"][0])
    box_d = roi_process(rpc, ll_poly, utm_zone=utm_zone)
    return box_d


def roi_process(rpc, ll_poly, utm_zone=None):
    """
    Convert a longitude/latitude polygon into a rectangular
    bounding box in image coordinates

    Args:
        rpc (rpcm.RPCModel): camera model
        ll_poly (array): 2D array of shape (n, 2) containing the vertices
            (longitude, latitude) of the polygon
        utm_zone: force the zone number to be used when defining `utm_bbx`.
            If not specified, the default UTM zone for the given geography
            is used.

    Returns:
        x, y, w, h: four integers defining a rectangular region of interest
            (ROI) in the image. (x, y) is the top-left corner, and (w, h)
            are the dimensions of the rectangle.
    """
    if not utm_zone:
        utm_zone = geographiclib.compute_utm_zone(*ll_poly.mean(axis=0))
    cfg["utm_zone"] = utm_zone

    # convert lon lat polygon to utm
    utm_proj = geographiclib.utm_proj(utm_zone)
    easting, northing = pyproj.transform(pyproj.Proj(init="epsg:4326"), utm_proj, ll_poly[:, 0], ll_poly[:, 1])
    east_min = min(easting)
    east_max = max(easting)
    nort_min = min(northing)
    nort_max = max(northing)
    cfg["utm_bbx"] = (east_min, east_max, nort_min, nort_max)

    # project lon lat vertices into the image
    img_pts = rpc.projection(ll_poly[:, 0], ll_poly[:, 1], rpc.alt_offset)
    img_pts = list(zip(*img_pts))

    # return image roi
    x, y, w, h = common.bounding_box2D(img_pts)
    return {"x": x, "y": y, "w": w, "h": h}


def generate_point_mesh(col_range, row_range, alt_range):
    """
    Generates image coordinates (col, row, alt) of 3D points located on the grid
    defined by col_range and row_range, at uniformly sampled altitudes defined
    by alt_range.
    Args:
        col_range: triplet (col_min, col_max, n_col), where n_col is the
            desired number of samples
        row_range: triplet (row_min, row_max, n_row)
        alt_range: triplet (alt_min, alt_max, n_alt)

    Returns:
        3 lists, containing the col, row and alt coordinates.
    """
    # input points in col, row, alt space
    cols, rows, alts = [np.linspace(v[0], v[1], v[2]) for v in [col_range, row_range, alt_range]]

    # make it a kind of meshgrid (but with three components)
    # if cols, rows and alts are lists of length 5, then after this operation
    # they will be lists of length 5x5x5
    cols, rows, alts = (
        (cols + 0 * rows[:, np.newaxis] + 0 * alts[:, np.newaxis, np.newaxis]).reshape(-1),
        (0 * cols + rows[:, np.newaxis] + 0 * alts[:, np.newaxis, np.newaxis]).reshape(-1),
        (0 * cols + 0 * rows[:, np.newaxis] + alts[:, np.newaxis, np.newaxis]).reshape(-1),
    )

    return cols, rows, alts


def ground_control_points(rpc, x, y, w, h, m, M, n):
    """
    Computes a set of ground control points (GCP), corresponding to RPC data.

    Args:
        rpc: instance of the rpcm.RPCModel class
        x, y, w, h: four integers defining a rectangular region of interest
            (ROI) in the image. (x, y) is the top-left corner, and (w, h) are
            the dimensions of the rectangle.
        m, M: minimal and maximal altitudes of the ground control points
        n: cube root of the desired number of ground control points.

    Returns:
        a list of world points, given by their geodetic (lon, lat, alt)
        coordinates.
    """
    # points will be sampled in [x, x+w] and [y, y+h]. To avoid always sampling
    # the same four corners with each value of n, we make these intervals a
    # little bit smaller, with a dependence on n.
    col_range = [x + (1.0 / (2 * n)) * w, x + ((2 * n - 1.0) / (2 * n)) * w, n]
    row_range = [y + (1.0 / (2 * n)) * h, y + ((2 * n - 1.0) / (2 * n)) * h, n]
    alt_range = [m, M, n]
    col, row, alt = generate_point_mesh(col_range, row_range, alt_range)
    lon, lat = rpc.localization(col, row, alt)
    return lon, lat, alt


def corresponding_roi(rpc1, rpc2, x, y, w, h):
    """
    Uses RPC functions to determine the region of im2 associated to the
    specified ROI of im1.

    Args:
        rpc1, rpc2: two instances of the rpcm.RPCModel class, or paths to
            the xml files
        x, y, w, h: four integers defining a rectangular region of interest
            (ROI) in the first view. (x, y) is the top-left corner, and (w, h)
            are the dimensions of the rectangle.

    Returns:
        four integers defining a ROI in the second view. This ROI is supposed
        to contain the projections of the 3D points that are visible in the
        input ROI.
    """
    # read rpc files
    if not isinstance(rpc1, rpcm.RPCModel):
        rpc1 = rpcm.RPCModel(rpc1)
    if not isinstance(rpc2, rpcm.RPCModel):
        rpc2 = rpcm.RPCModel(rpc2)
    m, M = altitude_range(rpc1, x, y, w, h, 0, 0)

    # build an array with vertices of the 3D ROI, obtained as {2D ROI} x [m, M]
    a = np.array([x, x, x, x, x + w, x + w, x + w, x + w])
    b = np.array([y, y, y + h, y + h, y, y, y + h, y + h])
    c = np.array([m, M, m, M, m, M, m, M])

    # corresponding points in im2
    xx, yy = find_corresponding_point(rpc1, rpc2, a, b, c)[0:2]

    # return coordinates of the bounding box in im2
    out = common.bounding_box2D(np.vstack([xx, yy]).T)
    return np.round(out)


def matches_from_rpc(rpc1, rpc2, x, y, w, h, n):
    """
    Uses RPC functions to generate matches between two Pleiades images.

    Args:
        rpc1, rpc2: two instances of the rpcm.RPCModel class
        x, y, w, h: four integers defining a rectangular region of interest
            (ROI) in the first view. (x, y) is the top-left corner, and (w, h)
            are the dimensions of the rectangle. In the first view, the matches
            will be located in that ROI.
        n: cube root of the desired number of matches.

    Returns:
        an array of matches, one per line, expressed as x1, y1, x2, y2.
    """
    m, M = altitude_range(rpc1, x, y, w, h, 100, -100)
    lon, lat, alt = ground_control_points(rpc1, x, y, w, h, m, M, n)
    x1, y1 = rpc1.projection(lon, lat, alt)
    x2, y2 = rpc2.projection(lon, lat, alt)

    return np.vstack([x1, y1, x2, y2]).T


def alt_to_disp(rpc1, rpc2, x, y, alt, H1, H2, A=None):
    """
    Converts an altitude into a disparity.

    Args:
        rpc1: an instance of the rpcm.RPCModel class for the reference
            image
        rpc2: an instance of the rpcm.RPCModel class for the secondary
            image
        x, y: coordinates of the point in the reference image
        alt: altitude above the WGS84 ellipsoid (in meters) of the point
        H1, H2: rectifying homographies
        A (optional): pointing correction matrix

    Returns:
        the horizontal disparity of the (x, y) point of im1, assuming that the
        3-space point associated has altitude alt. The disparity is made
        horizontal thanks to the two rectifying homographies H1 and H2.
    """
    xx, yy = find_corresponding_point(rpc1, rpc2, x, y, alt)[0:2]
    p1 = np.vstack([x, y]).T
    p2 = np.vstack([xx, yy]).T

    if A is not None:
        print("rpc_utils.alt_to_disp: applying pointing error correction")
        # correct coordinates of points in im2, according to A
        p2 = common.points_apply_homography(np.linalg.inv(A), p2)

    p1 = common.points_apply_homography(H1, p1)
    p2 = common.points_apply_homography(H2, p2)
    # np.testing.assert_allclose(p1[:, 1], p2[:, 1], atol=0.1)
    disp = p2[:, 0] - p1[:, 0]
    return disp


def exogenous_disp_range_estimation(rpc1, rpc2, x, y, w, h, H1, H2, A=None, margin_top=0, margin_bottom=0):
    """
    Args:
        rpc1: an instance of the rpcm.RPCModel class for the reference
            image
        rpc2: an instance of the rpcm.RPCModel class for the secondary
            image
        x, y, w, h: four integers defining a rectangular region of interest
            (ROI) in the reference image. (x, y) is the top-left corner, and
            (w, h) are the dimensions of the rectangle.
        H1, H2: rectifying homographies
        A (optional): pointing correction matrix
        margin_top: margin (in meters) to add to the upper bound of the range
        margin_bottom: margin (negative) to add to the lower bound of the range

    Returns:
        the min and max horizontal disparity observed on the 4 corners of the
        ROI with the min/max altitude assumptions given by the exogenous dem. The
        disparity is made horizontal thanks to the two rectifying homographies
        H1 and H2.
    """
    m, M = altitude_range(rpc1, x, y, w, h, margin_top, margin_bottom)

    return altitude_range_to_disp_range(m, M, rpc1, rpc2, x, y, w, h, H1, H2, A, margin_top, margin_bottom)


def altitude_range_to_disp_range(m, M, rpc1, rpc2, x, y, w, h, H1, H2, A=None, margin_top=0, margin_bottom=0):
    """
    Args:
        m: min altitude over the tile
        M: max altitude over the tile
        rpc1: instance of the rpcm.RPCModel class for the reference image
        rpc2: instance of the rpcm.RPCModel class for the secondary image
        x, y, w, h: four integers defining a rectangular region of interest
            (ROI) in the reference image. (x, y) is the top-left corner, and
            (w, h) are the dimensions of the rectangle.
        H1, H2: rectifying homographies
        A (optional): pointing correction matrix

    Returns:
        the min and max horizontal disparity observed on the 4 corners of the
        ROI with the min/max altitude assumptions given as parameters. The
        disparity is made horizontal thanks to the two rectifying homographies
        H1 and H2.
    """
    # build an array with vertices of the 3D ROI, obtained as {2D ROI} x [m, M]
    a = np.array([x, x, x, x, x + w, x + w, x + w, x + w])
    b = np.array([y, y, y + h, y + h, y, y, y + h, y + h])
    c = np.array([m, M, m, M, m, M, m, M])

    # compute the disparities of these 8 points
    d = alt_to_disp(rpc1, rpc2, a, b, c, H1, H2, A)

    # return min and max disparities
    return np.min(d), np.max(d)


def rpc_from_geotiff(geotiff_path):
    """
    Args:
        geotiff_path (str): path or url to a GeoTIFF file

    Return:
        instance of the rpcm.RPCModel class
    """
    with rasterio.open(geotiff_path, "r") as src:
        rpc_dict = src.tags(ns="RPC")
    return rpcm.RPCModel(rpc_dict)


def gsd_from_rpc(rpc):
    """
    Compute the ground sampling distance from an RPC camera model.

    Args:
        rpc (rpcm.RPCModel): camera model

    Returns:
        float (meters per pixel)
    """
    a = geographiclib.lonlat_to_geocentric(*rpc.localization(0, 0, 0), alt=0)
    b = geographiclib.lonlat_to_geocentric(*rpc.localization(1, 0, 0), alt=0)
    return np.linalg.norm(np.asarray(b) - np.asarray(a))


def camera_matrix(X, x):
    """
    Estimates the camera projection matrix from corresponding 3-space and image
    entities.

    Arguments:
        X: 2D array of size Nx3 containing the coordinates of the 3-space
            points, one point per line
        x: 2D array of size Nx2 containing the pixel coordinates of the imaged
            points, one point per line
            These two arrays are supposed to have the same number of lines.

    Returns:
        the estimated camera projection matrix, given by the Direct Linear
        Transformation algorithm, as described in Hartley & Zisserman book.
    """
    # normalize the input coordinates
    X, U = normalize_3d_points(X)
    x, T = normalize_2d_points(x)

    # make a linear system A*P = 0 from the correspondances, where P is made of
    # the 12 entries of the projection matrix (encoded in a vector P). This
    # system corresponds to the concatenation of correspondance constraints
    # (X_i --> x_i) which can be written as:
    # x_i x P*X_i = 0 (the vectorial product is 0)
    # and lead to 2 independent equations, for each correspondance. The system
    # is thus of size 2n x 12, where n is the number of correspondances. See
    # Zissermann, chapter 7, for more details.

    A = np.zeros((len(x) * 2, 12))
    for i in range(len(x)):
        A[2 * i + 0, 4:8] = -1 * np.array([X[i, 0], X[i, 1], X[i, 2], 1])
        A[2 * i + 0, 8:12] = x[i, 1] * np.array([X[i, 0], X[i, 1], X[i, 2], 1])
        A[2 * i + 1, 0:4] = np.array([X[i, 0], X[i, 1], X[i, 2], 1])
        A[2 * i + 1, 8:12] = -x[i, 0] * np.array([X[i, 0], X[i, 1], X[i, 2], 1])

    # the vector P we are looking for minimizes the norm of A*P, and satisfies
    # the constraint \norm{P}=1 (to avoid the trivial solution P=0). This
    # solution is obtained as the singular vector corresponding to the smallest
    # singular value of matrix A. See Zissermann for details.
    # It is the last line of matrix V (because np.linalg.svd returns V^T)
    W, S, V = np.linalg.svd(A)
    P = V[-1, :].reshape((3, 4))

    # denormalize P
    # implements P = T^-1 * P * U
    P = np.dot(np.dot(np.linalg.inv(T), P), U)
    return P


def normalize_2d_points(pts):
    """
    Translates and scales 2D points.

    The input points are translated and scaled such that the output points are
    centered at origin and the mean distance from the origin is sqrt(2). As
    shown in Hartley (1997), this normalization process typically improves the
    condition number of the linear systems used for solving homographies,
    fundamental matrices, etc.

    References:
        Richard Hartley, PAMI 1997
        Peter Kovesi, MATLAB functions for computer vision and image processing,

    Args:
        pts: 2D array of dimension Nx2 containing the coordinates of the input
            points, one point per line

    Returns:
        new_pts, T: coordinates of the transformed points, together with
            the similarity transform
    """
    # centroid
    cx = np.mean(pts[:, 0])
    cy = np.mean(pts[:, 1])

    # shift origin to centroid
    new_x = pts[:, 0] - cx
    new_y = pts[:, 1] - cy

    # scale such that the average distance from centroid is \sqrt{2}
    mean_dist = np.mean(np.sqrt(new_x ** 2 + new_y ** 2))
    s = np.sqrt(2) / mean_dist
    new_x = s * new_x
    new_y = s * new_y

    # matrix T           s     0   -s * cx
    # is given     T  =  0     s   -s * cy
    # by                 0     0    1
    T = np.eye(3)
    T[0, 0] = s
    T[1, 1] = s
    T[0, 2] = -s * cx
    T[1, 2] = -s * cy

    return np.vstack([new_x, new_y]).T, T


def normalize_3d_points(pts):
    """
    Translates and scales 3D points.

    The input points are translated and scaled such that the output points are
    centered at origin and the mean distance from the origin is sqrt(3).

    Args:
        pts: 2D array of dimension Nx3 containing the coordinates of the input
            points, one point per line

    Returns:
        new_pts, U: coordinates of the transformed points, together with
            the similarity transform
    """
    # centroid
    cx = np.mean(pts[:, 0])
    cy = np.mean(pts[:, 1])
    cz = np.mean(pts[:, 2])

    # shift origin to centroid
    new_x = pts[:, 0] - cx
    new_y = pts[:, 1] - cy
    new_z = pts[:, 2] - cz

    # scale such that the average distance from centroid is \sqrt{3}
    mean_dist = np.mean(np.sqrt(new_x ** 2 + new_y ** 2 + new_z ** 2))
    s = np.sqrt(3) / mean_dist
    new_x = s * new_x
    new_y = s * new_y
    new_z = s * new_z

    # matrix U             s     0      0    -s * cx
    # is given             0     s      0    -s * cy
    # by this        U  =  0     0      s    -s * cz
    # formula              0     0      0     1

    U = np.eye(4)
    U[0, 0] = s
    U[1, 1] = s
    U[2, 2] = s
    U[0, 3] = -s * cx
    U[1, 3] = -s * cy
    U[2, 3] = -s * cz

    return np.vstack([new_x, new_y, new_z]).T, U
