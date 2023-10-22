from math import atan2, cos, degrees, pi, radians, sin, sqrt
import shapely
import movingpandas as mpd
from geopy import distance
from geopy.distance import geodesic
from packaging.version import Version
from shapely.geometry import LineString, Point
import time
import numpy as np
import geopandas as gpd
import pandas as pd
from scipy.sparse import coo_matrix
from collections import OrderedDict


def calculate_initial_compass_bearing(point1, point2):
    """
    Calculate the bearing between two points.

    The formulae used is the following:
        θ = atan2(sin(Δlong).cos(lat2),
                  cos(lat1).sin(lat2) − sin(lat1).cos(lat2).cos(Δlong))
    :Parameters:
      - `point1: shapely Point
      - `point2: shapely Point
    :Returns:
      The bearing in degrees
    :Returns Type:
      float
    """
    lat1 = radians(point1.y)
    lat2 = radians(point2.y)
    delta_lon = radians(point2.x - point1.x)
    x = sin(delta_lon) * cos(lat2)
    y = cos(lat1) * sin(lat2) - (sin(lat1) * cos(lat2) * cos(delta_lon))
    initial_bearing = atan2(x, y)
    # Now we have the initial bearing but math.atan2 return values
    # from -180° to + 180° which is not what we want for a compass bearing
    # The solution is to normalize the initial bearing as shown below
    initial_bearing = degrees(initial_bearing)
    compass_bearing = (initial_bearing + 360) % 360

    return compass_bearing

def compass_mean(bearings1, bearings2):
    # Convert compass bearings to radians
    rad_bearings1 = np.radians(bearings1)
    rad_bearings2 = np.radians(bearings2)

    # Convert bearings to complex numbers on the unit circle
    complex_bearings1 = np.exp(1j * rad_bearings1)
    complex_bearings2 = np.exp(1j * rad_bearings2)

    # Calculate the mean complex bearing
    mean_complex_bearing = (complex_bearings1 + complex_bearings2) / 2

    # Calculate the mean bearing in degrees
    mean_bearing = np.degrees(np.angle(mean_complex_bearing)) % 360

    return mean_bearing

def find_orig_WP(points, waypoints):
    '''
    Given a trajectory, find the closest waypoint to the start of the trajectory
    '''
    orig = points.iloc[0].geometry  # get trajectory start point
    # find out if trajectory starts in a stop point
    try:
        orig_speed = points.iloc[0:5].speed.mean()
        if orig_speed < 2:
            orig_cog = calculate_initial_compass_bearing(Point(points.iloc[0].lon, points.iloc[0].lat), 
                                                         Point(points.iloc[40].lon, points.iloc[40].lat)) # get initial cog
            angle = (orig_cog - waypoints.cog_after + 180) % 360 - 180
            mask = ((waypoints.speed < 2) & (np.abs(angle) < 45))
            #mask = np.abs(angle) < 45
        else:
            orig_cog = calculate_initial_compass_bearing(Point(points.iloc[0].lon, points.iloc[0].lat), 
                                                         Point(points.iloc[9].lon, points.iloc[9].lat)) # get initial cog
            angle = (orig_cog - waypoints.cog_after + 180) % 360 - 180
            mask = np.abs(angle) < 45  # only consider waypoints that have similar direction
    except:
        orig_speed = points.iloc[0:2].speed.mean()
        orig_cog = geometry_utils.calculate_initial_compass_bearing(Point(points.iloc[0].lon, points.iloc[0].lat), 
                                                                        Point(points.iloc[1].lon, points.iloc[1].lat)) # get initial cog
        angle = (orig_cog - waypoints.cog_after + 180) % 360 - 180
        mask = np.abs(angle) < 45  # only consider waypoints that have similar direction
    distances = orig.distance(waypoints[mask].geometry)
    masked_idx = np.argmin(distances)
    orig_WP = waypoints[mask]['clusterID'].iloc[masked_idx]
    # find trajectory point that is closest to the centroid of the first waypoint
    # this is where we start measuring
    orig_WP_point = waypoints[waypoints.clusterID==orig_WP]['geometry'].item()
    idx_orig = np.argmin(orig_WP_point.distance(points.geometry))
    
    return orig_WP, idx_orig

def find_dest_WP(points, waypoints):
    dest = points.iloc[-1].geometry  # get end point
    try:
        dest_speed = points.iloc[-5:-1].speed.mean()
        if dest_speed < 2:
            mask = (waypoints.speed < 2)
        else:
            dest_cog = calculate_initial_compass_bearing(Point(points.iloc[-10].lon, points.iloc[-10].lat), 
                                                         Point(points.iloc[-1].lon, points.iloc[-1].lat)) # get initial cog
            angle = (dest_cog - waypoints.cog_before + 180) % 360 - 180
            mask = np.abs(angle) < 45  # only consider waypoints that have similar direction
    except:
        dest_speed = points.iloc[-3:-1].speed.mean()
        dest_cog = calculate_initial_compass_bearing(Point(points.iloc[-2].lon, points.iloc[-2].lat), 
                                                     Point(points.iloc[-1].lon, points.iloc[-1].lat)) # get initial cog
        angle = (dest_cog - waypoints.cog_before + 180) % 360 - 180
        mask = np.abs(angle) < 45  # only consider waypoints that have similar direction
    distances = dest.distance(waypoints[mask].geometry)
    masked_idx = np.argmin(distances)
    dest_WP = waypoints[mask]['clusterID'].iloc[masked_idx]
    # find trajectory point that is closest to the centroid of the last waypoint
    # this is where we end measuring
    dest_WP_point = waypoints[waypoints.clusterID==dest_WP]['geometry'].item()
    idx_dest = np.argmin(dest_WP_point.distance(points.geometry))
    return dest_WP, idx_dest

def find_WP_intersections(trajectory, waypoints):
    '''
    given a trajectory, find all waypoint intersections in the correct order
    '''
    max_distance = 50
    max_angle = 30
    # simplofy trajectory
    simplified_trajectory = mpd.DouglasPeuckerGeneralizer(trajectory).generalize(tolerance=10)
    simplified_trajectory.add_direction()
    trajectory_segments = simplified_trajectory.to_line_gdf()
    # filter waypoints: only consider waypoints within a certain distance to the trajectory
    distances = trajectory.distance(waypoints['convex_hull'])
    mask = distances < max_distance
    close_wps = waypoints[mask]
    passages = []  # initialize ordered list of waypoint passages per line segment
    for i in range(0, len(trajectory_segments)-1):
        segment = trajectory_segments.iloc[i]
        # distance of each segment to the selection of close waypoints
        distance_to_line = segment['geometry'].distance(close_wps.convex_hull)  # distance between line segment and waypoint convex hull  
        distance_to_origin = segment['geometry'].boundary.geoms[0].distance(close_wps.geometry)  # distance between first point of segment and waypoint centroids (needed for sorting)
        close_wps['distance_to_line'] = distance_to_line.tolist()
        close_wps['distance_to_origin'] = distance_to_origin.tolist()
        # angle between line segment and mean traffic direction in each waypoint
        WP_cog_before = close_wps['cog_before'] 
        WP_cog_after  = close_wps['cog_after']
        trajectory_cog = segment['direction']
        #print('WP COG before: ', WP_cog_before, 'WP COG after: ', WP_cog_after, 'Trajectory COG: ', trajectory_cog)
        close_wps['angle_before'] = np.abs(WP_cog_before - trajectory_cog + 180) % 360 - 180
        close_wps['angle_after'] = np.abs(WP_cog_after - trajectory_cog + 180) % 360 - 180
        # the line segment is associated with the waypoint, when its distance and angle is less than a threshold
        mask = ((close_wps['distance_to_line']<max_distance) & 
                (np.abs(close_wps['angle_before'])<max_angle) & 
                (np.abs(close_wps['angle_after'])<max_angle))
        passed_wps = close_wps[mask]
        passed_wps.sort_values(by='distance_to_origin', inplace=True)
        passages.extend(passed_wps['clusterID'].tolist())
    
    return list(OrderedDict.fromkeys(passages))

def LEGACY_aggregate_edges(waypoints, waypoint_connections):
    # refine the graph
    # each edge that intersects the convex hull of another waypoint is divided in segments
    # the segments are added to the adjacency matrix and the original edge is deleted
    print('Aggregating graph edges...')
    start = time.time()  # start timer
    
    n_clusters = len(waypoints)
    coord_dict = {}
    flag = True
    for i in range(0, len(waypoint_connections)):
        edge = waypoint_connections['geometry'].iloc[i]
        mask = edge.intersects(waypoints['convex_hull'])
        subset = waypoints[mask][['clusterID', 'cog_before', 'cog_after', 'geometry']]
        # drop waypoints with traffic direction that does not match edge direction
        subset = subset[np.abs((subset.cog_before + subset.cog_after)/2 - waypoint_connections.direction.iloc[i]) < 30]
        # When we find intersections that match the direction of the edge, we split the edge
        if len(subset)>2:
            # sort by distance
            start_point = edge.boundary.geoms[0]
            subset['distance'] = start_point.distance(subset['geometry'])
            subset.sort_values(by='distance', inplace=True)
            # split line in two segments
            # first segment: between start point and next closest point
            row = subset.clusterID.iloc[0]
            col = subset.clusterID.iloc[1]
            if (row, col) in coord_dict:
                coord_dict[(row, col)] += waypoint_connections['passages'].iloc[i]  # increase the edge weight for each passage
            else:
                coord_dict[(row, col)] = waypoint_connections['passages'].iloc[i]  # create edge if it does not exist yet
            # second segment: between next clostest point and endpoint
            row = subset.clusterID.iloc[1]
            col = waypoint_connections['to'].iloc[i]
            if (row, col) in coord_dict:
                coord_dict[(row, col)] += waypoint_connections['passages'].iloc[i]  # increase the edge weight for each passage
            else:
                coord_dict[(row, col)] = waypoint_connections['passages'].iloc[i]  # create edge if it does not exist yet
        # When we don't find intersections, we keep the original edge
        else:
            row = waypoint_connections['from'].iloc[i]
            col = waypoint_connections['to'].iloc[i]
            if (row, col) in coord_dict:
                coord_dict[(row, col)] += waypoint_connections['passages'].iloc[i]  # increase the edge weight for each passage
            else:
                coord_dict[(row, col)] = waypoint_connections['passages'].iloc[i]  # create edge if it does not exist yet
    
    # create refined adjacency matrix
    row_indices, col_indices = zip(*coord_dict.keys())
    values = list(coord_dict.values())
    A_refined = coo_matrix((values, (row_indices, col_indices)), shape=(n_clusters, n_clusters))
    
    waypoints.set_geometry('geometry', inplace=True)
    waypoint_connections_refined = pd.DataFrame(columns=['from', 'to', 'geometry', 'direction', 'passages'])
    for orig, dest, weight in zip(A_refined.row, A_refined.col, A_refined.data):
        # add linestring as edge
        p1 = waypoints[waypoints.clusterID == orig].geometry
        p2 = waypoints[waypoints.clusterID == dest].geometry
        edge = LineString([(p1.x, p1.y), (p2.x, p2.y)])
        # compute the orientation fo the edge (COG)
        p1 = Point(waypoints[waypoints.clusterID == orig].lon, waypoints[waypoints.clusterID == orig].lat)
        p2 = Point(waypoints[waypoints.clusterID == dest].lon, waypoints[waypoints.clusterID == dest].lat)
        direction = calculate_initial_compass_bearing(p1, p2)
        line = pd.DataFrame([[orig, dest, edge, direction, weight]], 
                            columns=['from', 'to', 'geometry', 'direction', 'passages'])
        waypoint_connections_refined = pd.concat([waypoint_connections_refined, line])
    # save result
    waypoint_connections_refined = gpd.GeoDataFrame(waypoint_connections_refined, geometry='geometry', crs=waypoints.crs)
    
    end = time.time()  # end timer
    print(f'Aggregated {len(waypoint_connections)} edges to {len(waypoint_connections_refined)} edges (Time elapsed: {(end-start)/60:.2f} minutes)')
    if len(waypoint_connections) == len(waypoint_connections_refined):
        flag = False
        print(f'Edge aggregation finished.')
    
    return A_refined, waypoint_connections_refined, flag