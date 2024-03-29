import pandas as pd
import geopandas as gpd
import movingpandas as mpd
import numpy as np
import matplotlib.pyplot as plt
from datetime import timedelta, datetime
from sklearn.cluster import DBSCAN, HDBSCAN, OPTICS
from scipy.sparse import coo_matrix
from scipy.stats import halfnorm, skew, kurtosis
from shapely.geometry import Point, LineString, MultiLineString
from leuvenmapmatching.matcher.distance import DistanceMatcher
from leuvenmapmatching.map.inmem import InMemMap
from shapely import ops
from collections import OrderedDict
import networkx as nx
import sparse
import time
import folium
import warnings
import sys

# add paths for modules
sys.path.append('../visualization')
sys.path.append('../features')

# import modules
import visualize
import geometry_utils

class MaritimeTrafficNetwork:
    '''
    A class for modelling a maritime traffic network (MTN)
    The network is extracted from a set of trajectories contained in the dataframe gdf according to the following steps:
    - the method 'calc_significant_points_DP' computes significant turning points from the set of trajectories
    - the method 'calc_waypoints_clustering' computes waypoints through clustering of significant points
    - the method 'make_graph_from_waypoints' connects the waypoints and yields a maritime traffic network graph
    Additional methods allow us to refine the MTN:
    - the method 'merge_stop_points' merges overlapping waypoints in port areas or anchorages which simplifies the network slightly
    - the method 'prune_graph' removes edges according to specified criteria to simplify the network
    - the method 'refine_graph_from_paths' computes useful network features and refines the graph based on a set of observed trajectories
    We can evaluate the network for its goodness of fit with the following methods:
    - the methods 'trajectory_to_path_sspd' and 'map_matching' are two alternative algorithms to map observed trajectories to paths on the graph
    - given the mapped paths, we can compute network evaluation metrics using the method 'evaluate_graph'
    '''
    def __init__(self, gdf, crs):
        self.gdf = gdf                                # GeoDataframe containing the processed AIS data for MTN generation
        self.crs = crs                                # Coordinate reference system
        self.hyperparameters = {}                     # MTN hyperparameters
        self.trajectories = mpd.TrajectoryCollection(self.gdf, traj_id_col='mmsi', obj_id_col='mmsi')
        self.significant_points_trajectory = []       # significant turning points, represented as trajectories per vessel
        self.significant_points = []                  # significant turning points
        self.waypoints = []                           # MTN waypoints, represented as a GeoDataframe
        self.waypoint_connections = []                # MTN edges, represented as a GeoDataframe
        self.waypoint_connections_pruned = []         # pruned MTN edges, represented as a GeoDataframe
        self.waypoint_connections_refined = []        # refined MTN edges (with additional features), represented as a GeoDataframe
        self.G = []                                   # MTN represented as a  networkx graph
        self.G_pruned = []                            # pruned MTN represented as a  networkx graph
        self.G_refined = []                           # refined MTN represented as a  networkx graph

    def get_trajectories_info(self):
        # print info about underlying trajectories
        print(f'Number of AIS messages: {len(self.gdf)}')
        print(f'Number of trajectories: {len(self.trajectories)}')
        print(f'Coordinate Reference System (CRS): {self.gdf.crs}')

    def set_hyperparameters(self, params):
        # set MTN hyperparameters
        self.hyperparameters = params
    
    def init_precomputed_significant_points(self, gdf):
        # Load precomputed significant points from a GeoDataframe
        print('Loading significant turning points from file...')
        self.significant_points = gdf
        self.significant_points_trajectory = mpd.TrajectoryCollection(gdf, traj_id_col='mmsi', obj_id_col='mmsi', t='date_time_utc')
        n_points, n_DP_points = len(self.gdf), len(self.significant_points)
        print(f'Number of significant points detected: {n_DP_points} ({n_DP_points/n_points*100:.2f}% of AIS messages)')

    def init_precomputed_waypoints(self, gdf):
        # Load precomputed waypoints from a GeoDataframe
        print('Loading precomputed waypoints from file...')
        self.waypoints = gdf
        print(f'{len(gdf)} waypoints loaded')
    
    def calc_significant_points_DP(self, tolerance):
        '''
        This method detects significant turning points with the Douglas Peucker algorithm 
        ====================================
        Params:
        tolerance: (float) Douglas Peucker algorithm tolerance parameter (for UTM coordinate reference system, the unit is meters)
        ====================================
        result: self.significant_points_trajectory is set to a MovingPandas TrajectoryCollection containing
                the significant turning points
                self.significant_points is set to GeoPandasDataframe containing the significant turning 
                points and COG information
        '''
        print(f'Calculating significant turning points with Douglas Peucker algorithm (tolerance = {tolerance}) ...')
        start = time.time()  # start timer
        
        # Compute significant points with Douglas Peucker algorithm
        self.significant_points_trajectory = mpd.DouglasPeuckerGeneralizer(self.trajectories).generalize(tolerance=tolerance)
        n_points, n_DP_points = len(self.gdf), len(self.significant_points_trajectory.to_point_gdf())
        
        end = time.time()  # end timer
        print(f'Number of significant points detected: {n_DP_points} ({n_DP_points/n_points*100:.2f}% of AIS messages)')
        print(f'Time elapsed: {(end-start)/60:.2f} minutes')

        # Add course over ground (COG) before and after each turning point
        print(f'Adding course over ground before and after each turn ...')
        start = time.time()  # start timer
        
        self.significant_points_trajectory.add_direction()

        # Reformat and assign results
        traj_df = self.significant_points_trajectory.to_point_gdf()
        traj_df.rename(columns={'direction': 'cog_before'}, inplace=True)
        traj_df['cog_after'] = np.nan
        for mmsi in traj_df.mmsi.unique():
            subset = traj_df[traj_df.mmsi == mmsi]
            if len(subset)>1:
                fill_value = subset['cog_before'].iloc[-1]
                subset['cog_after'] = subset['cog_before'].shift(-1, fill_value=fill_value)
            else:
                subset['cog_after'] = subset['cog_before']
            traj_df.loc[traj_df.mmsi == mmsi, 'cog_after'] = subset['cog_after'].values 
        self.significant_points = traj_df
        end = time.time()  # end timer
        print(f'Done. Time elapsed: {(end-start)/60:.2f} minutes')

    def calc_waypoints_clustering(self, method='HDBSCAN', min_samples=15, min_cluster_size=15, eps=50, 
                                  metric='euclidean', V=np.diag([1, 1, 0.01, 0.01, 1])):
        '''
        This method computes the waypoints of the MTN through clustering of significant turning points
        ====================================
        Params: 
        method: (str) Clustering method (supported: 'DBSCAN', 'HDBSCAN', 'OPTICS')
        min_points: (int) hyperparameter for DBSCAN and HDBSCAN
        min_cluster_size: (int) hyperparameter for DBSCAN and HDBSCAN
        eps: (float) hyperparameter for DBSCAN
        tolerance: (float) Douglas Peucker algorithm tolerance parameter (for UTM coordinate reference system, the unit is meters)
        metric: (str) distance metric for clustering (supported: 'euclidean', 'mahalanobis', 'haversine')
        V: (numpy array) hyperparameter for mahalanobis distance (scaling matrix)
        ====================================
        result: self.waypoints is assigned a GeoDataframe containing the extracted clusters including features 
        '''
        start = time.time()  # start timer
        
        # prepare data for clustering
        significant_points = self.significant_points
        significant_points['x'] = significant_points.geometry.x
        significant_points['y'] = significant_points.geometry.y

        # prepare clustering depending on metric
        if metric == 'euclidean':
            columns = ['x', 'y']
            metric_params = {}
        elif metric == 'haversine':
            columns = ['x', 'y']
            metric_params = {}
        elif metric == 'mahalanobis':
            columns = ['x', 'y', 'cog_before', 'cog_after', 'speed']
            metric_params = {'V':V}   # mahalanobis distance parameter matrix
            metric_params_OPTICS = {'VI':np.linalg.inv(V)}
        else:
            print(f'{metric} is not a supported distance metric. Exiting waypoint calculation...')
            return
        
        ########
        # DBSCAN
        ########
        if method == 'DBSCAN':
            print(f'Calculating waypoints with {method} (eps = {eps}, min_samples = {min_samples}) ...')
            print(f'Distance metric: {metric}')
            clustering = DBSCAN(eps=eps, min_samples=min_samples, 
                                metric=metric, metric_params=metric_params).fit(significant_points[columns])   
        #########
        # HDBSCAN
        #########
        elif method == 'HDBSCAN':
            print(f'Calculating waypoints with {method} (min_samples = {min_samples}) ...')
            print(f'Distance metric: {metric}')
            clustering = HDBSCAN(min_cluster_size=min_cluster_size, min_samples=min_samples, 
                                 cluster_selection_method='leaf', metric=metric, 
                                 metric_params=metric_params).fit(significant_points[columns])
        #########
        # OPTICS
        #########
        elif method == 'OPTICS':
            print(f'Calculating waypoints with {method} (min_samples = {min_samples}) ...')
            print(f'Distance metric: {metric}')
            clustering = OPTICS(min_samples=min_samples, metric=metric, 
                                metric_params=metric_params_OPTICS).fit(significant_points[columns])
        else:
            print(f'{method} is not a supported clustering method. Exiting waypoint calculation...')
            return
        
        # compute cluster centroids and additional features for each cluster
        cluster_centroids = pd.DataFrame(columns=['clusterID', 'lat', 'lon', 'x', 'y', 
                                                  'speed', 'cog_before', 'cog_after', 'n_members', 'convex_hull'])
        for i in range(0, max(clustering.labels_)+1):
            lat = significant_points[clustering.labels_ == i].lat.mean()
            lon = significant_points[clustering.labels_ == i].lon.mean()
            x = significant_points[clustering.labels_ == i].x.mean()
            y = significant_points[clustering.labels_ == i].y.mean()
            speed = significant_points[clustering.labels_ == i].speed.mean()
            cog_before = geometry_utils.mean_bearing(significant_points[clustering.labels_ == i].cog_before.tolist())
            cog_after = geometry_utils.mean_bearing(significant_points[clustering.labels_ == i].cog_after.tolist())
            n_members = len(significant_points[clustering.labels_ == i])
            centroid = pd.DataFrame([[i, lat, lon, x, y, speed, cog_before, cog_after, n_members]], 
                                    columns=['clusterID', 'lat', 'lon', 'x', 'y', 
                                             'speed', 'cog_before', 'cog_after', 'n_members'])
            cluster_centroids = pd.concat([cluster_centroids, centroid])
        
        significant_points['clusterID'] = clustering.labels_  # assign clusterID to each waypoint
        
        # convert waypoint and cluster centroid DataFrames to GeoDataFrames
        significant_points = gpd.GeoDataFrame(significant_points, 
                                              geometry=gpd.points_from_xy(significant_points.x,
                                                                          significant_points.y), 
                                              crs=self.crs)
        significant_points.reset_index(inplace=True)
        cluster_centroids = gpd.GeoDataFrame(cluster_centroids, 
                                             geometry=gpd.points_from_xy(cluster_centroids.x,
                                                                         cluster_centroids.y),
                                             crs=self.crs)
        
        # compute convex hull of each cluster
        for i in range(0, max(clustering.labels_)+1):
            hull = significant_points[significant_points.clusterID == i].unary_union.convex_hull
            cluster_centroids['convex_hull'].iloc[i] = hull
        
        # print results
        print(f'{len(cluster_centroids)} clusters detected')
        end = time.time()  # end timer
        print(f'Time elapsed: {(end-start)/60:.2f} minutes')
        
        # assign results
        self.significant_points = significant_points
        self.waypoints = cluster_centroids

    def merge_stop_points(self, max_speed=2):
        '''
        Merges stop points together that intersect with their convex hull
        Stop points are waypoints where the average vessel spped is below the threshold of max_speed
        ====================================
        Params:
        max_speed: (float) Speed threshold (for UTM the unit is m/s)
        ====================================
        '''
        # Identify stop points based on average speed
        waypoints = self.waypoints.copy()
        stop_points = waypoints[waypoints['speed'] < max_speed]
        columns = stop_points.columns.tolist()
        columns.append('members')
        
        merged_stop_points = pd.DataFrame(columns=columns)  # new DataFrame containing the merged stop points
        drop_IDs = []  # list of waypoint IDs that will be dropped, because they were merged
        
        # iterate over all stop points
        for i in range(0, len(stop_points)):
            current_stop_point = stop_points.iloc[i]
            # find other stop points that intersect with the current stop point
            mask = current_stop_point['convex_hull'].intersects(stop_points['convex_hull'])
            intersect_stop_points = stop_points[mask]
            members = intersect_stop_points['clusterID'].unique()
            drop_IDs.append(members.tolist())  # add IDs of redundant stop points to list
            convex_hull = ops.unary_union(intersect_stop_points['convex_hull'])  # merge the convex hulls of intersecting stop points
            clusterID = members[0]  # assign the ID of the first stop point as new ID for the merged stop point
            # get merged stop point attributes
            lat = current_stop_point['lat']
            lon = current_stop_point['lon']
            x = current_stop_point['x']
            y = current_stop_point['y']
            speed = intersect_stop_points['speed'].mean()
            n_members = intersect_stop_points['n_members'].sum()
            geometry = Point(x, y)
            # if the stop point does not exist yet, append it to a new DataFrame with merged stop points
            if clusterID not in merged_stop_points['clusterID'].unique():
                line = pd.DataFrame([[clusterID, lat, lon, x, y, speed, 0, 0, n_members, convex_hull, geometry, members]], 
                                    columns=columns)  # we set the course in the stop point to 0, as it does not matter
                merged_stop_points = pd.concat([merged_stop_points, line])
        merged_stop_points = gpd.GeoDataFrame(merged_stop_points, geometry='geometry', crs=self.crs)
        
        # drop all stop points from waypoints DataFrame
        drop_IDs = set(item for sublist in drop_IDs for item in sublist)
        drop_IDs = list(drop_IDs)
        mask = ~waypoints['clusterID'].isin(drop_IDs)
        new_waypoints = waypoints[mask]
        # append merged stop points to waypoints DataFrame
        new_waypoints = pd.concat([new_waypoints, merged_stop_points[new_waypoints.columns]])
        new_waypoints = gpd.GeoDataFrame(new_waypoints, geometry='geometry', crs=self.crs)
        
        # adjust waypoint connections
        waypoint_connections = self.waypoint_connections.copy()
        drop_IDs = []
        # for each merged stop point, assign all connections from and to original stop points
        for i in range(0, len(merged_stop_points)):
            merged_stop_point = merged_stop_points.iloc[i]
            merge_nodes = merged_stop_point['members']
            # iterate over all stop points that compose the merged stop point
            for j in range(1, len(merge_nodes)):
                # outgoing connections
                from_connections = waypoint_connections[waypoint_connections['from']==merge_nodes[j]]  # all outgoing connections of the original stop point
                # iterate over all outgoing connections
                for k in range(0, len(from_connections)):
                    add_passages_from = from_connections['passages'].iloc[k]  # number of passages through that connection
                    # no self loops
                    if merge_nodes[0] == from_connections['to'].iloc[k]:
                        continue
                    mask = ((waypoint_connections['from']==merge_nodes[0]) &
                           (waypoint_connections['to']==from_connections['to'].iloc[k]))
                    # if connection already exists: add the number of passages to that connection
                    if len(waypoint_connections[mask]) > 0:
                        waypoint_connections[mask]['passages'] += add_passages_from
                    # if the connection does not exist: add new connection
                    else:
                        # add linestring as edge
                        orig = merge_nodes[0]
                        dest = from_connections['to'].iloc[k]
                        p1 = new_waypoints[new_waypoints.clusterID == orig]['geometry']
                        p2 = waypoints[waypoints.clusterID == dest]['geometry']
                        edge = LineString([(p1.x, p1.y), (p2.x, p2.y)])
                        length = edge.length
                        # compute the orientation fo the edge (COG)
                        p1 = Point(new_waypoints[new_waypoints.clusterID == orig].lon, new_waypoints[new_waypoints.clusterID == orig].lat)
                        p2 = Point(waypoints[waypoints.clusterID == dest].lon, waypoints[waypoints.clusterID == dest].lat)
                        direction = geometry_utils.calculate_initial_compass_bearing(p1, p2)
                        line = pd.DataFrame([[orig, dest, edge, direction, length, add_passages_from]], 
                                            columns=['from', 'to', 'geometry', 'direction', 'length', 'passages'])
                        waypoint_connections = pd.concat([waypoint_connections, line])
                
                #incoming connections
                to_connections = waypoint_connections[waypoint_connections['to']==merge_nodes[j]]
                # iterate over all incoming connections
                for k in range(0, len(to_connections)):
                    add_passages_to = to_connections['passages'].iloc[k]  # number of passages through that connection
                    # no self loops
                    if merge_nodes[0] == to_connections['from'].iloc[k]:
                        continue
                    mask = ((waypoint_connections['to']==merge_nodes[0]) &
                           (waypoint_connections['from']==to_connections['from'].iloc[k]))
                    # if connection already exists: add the number of passages to that connection
                    if  len(waypoint_connections[mask]) > 0:
                        waypoint_connections[mask]['passages'] += add_passages_to
                    # if the connection does not exist: add new connection
                    else:
                        # add linestring as edge
                        dest = merge_nodes[0]
                        orig = to_connections['from'].iloc[k]
                        p1 = waypoints[waypoints.clusterID == orig]['geometry']
                        p2 = new_waypoints[new_waypoints.clusterID == dest]['geometry']
                        edge = LineString([(p1.x, p1.y), (p2.x, p2.y)])
                        length = edge.length
                        # compute the orientation fo the edge (COG)
                        p1 = Point(waypoints[waypoints.clusterID == orig].lon, waypoints[waypoints.clusterID == orig].lat)
                        p2 = Point(new_waypoints[new_waypoints.clusterID == dest].lon, new_waypoints[new_waypoints.clusterID == dest].lat)
                        direction = geometry_utils.calculate_initial_compass_bearing(p1, p2)
                        line = pd.DataFrame([[orig, dest, edge, direction, length, add_passages_to]], 
                                            columns=['from', 'to', 'geometry', 'direction', 'length', 'passages'])
                        waypoint_connections = pd.concat([waypoint_connections, line])
                drop_IDs.append(merge_nodes[j])  # append original stop point to list of stop points to drop
        # drop all 'old' connections
        mask = ~(waypoint_connections['to'].isin(drop_IDs) | waypoint_connections['from'].isin(drop_IDs)) 
        new_waypoint_connections = waypoint_connections[mask]
        new_waypoint_connections = gpd.GeoDataFrame(new_waypoint_connections, geometry='geometry', crs=self.crs)

        # make graph from waypoints and waypoint connections
        # add node features
        G = nx.DiGraph()
        for i in range(0, len(new_waypoints)):
            node_id = new_waypoints['clusterID'].iloc[i]
            G.add_node(node_id)
            G.nodes[node_id]['n_members'] = new_waypoints.n_members.iloc[i]
            G.nodes[node_id]['position'] = (new_waypoints.lon.iloc[i], new_waypoints.lat.iloc[i])  # !changed lat-lon to lon-lat for plotting
            G.nodes[node_id]['speed'] = new_waypoints.speed.iloc[i]
            G.nodes[node_id]['cog_before'] = new_waypoints.cog_before.iloc[i]
            G.nodes[node_id]['cog_after'] = new_waypoints.cog_after.iloc[i]
        
        for i in range(0, len(new_waypoint_connections)):
            orig = new_waypoint_connections['from'].iloc[i]
            dest = new_waypoint_connections['to'].iloc[i]
            e = (orig, dest)
            G.add_edge(*e)
            G[orig][dest]['weight'] = new_waypoint_connections['passages'].iloc[i]
            G[orig][dest]['length'] = new_waypoint_connections['length'].iloc[i]
            G[orig][dest]['direction'] = new_waypoint_connections['direction'].iloc[i]
            G[orig][dest]['geometry'] = new_waypoint_connections['geometry'].iloc[i]
            G[orig][dest]['inverse_weight'] = 1/new_waypoint_connections['passages'].iloc[i]

        self.waypoints = new_waypoints
        self.waypoint_connections = new_waypoint_connections
        self.G = G
    
    def prune_graph(self, min_passages, graph='original'):
        '''
        Prune the MTN by removing edges 
        An edge between nodes u and v is removed when
            ... it violates the traffic directions of both waypoints that it connects
            ... it has a weight < min_passages and there is an alternative path between u and v shorter than 5 edges
        ====================================
        Params:
        min_passages: (int) minimum number of passages along an edge required for the edge to be retained
        graph: (str) 'original' (prunes the original MTN graph) or 'refined' (prunes the refined MTN graph). 
        ====================================
        '''
        print('Pruning...')
        # Decide which graph to prune, either the original or the refined graph
        if graph == 'original':
            G_pruned = self.G.copy()
            G_temp = self.G.copy()
            connections_pruned = self.waypoint_connections.copy()
        elif graph == 'refined':
            G_pruned = self.G_refined.copy()
            G_temp = self.G_refined.copy()
            connections_pruned = self.waypoint_connections_refined.copy()
        else:
            print('Specify a valid graph to prune. Either "original" for the original graph or "refined" for the refined graph.')
            return

        edges_to_remove = []  # placeholder for edges that will be removed
        
        # remove all edges that violate the main traffic flow through waypoints
        for u, v in G_pruned.edges():
            # get edge direction
            edge_direction = G_pruned[u][v]['direction']
            # get waypoint traffic directions
            try:
                WP1_direction = G_pruned.nodes[u]['cog_after']
                WP2_direction = G_pruned.nodes[v]['cog_before']
                # compute angles
                angle1 = np.abs(WP1_direction - edge_direction + 180) % 360 - 180
                angle2 = np.abs(WP2_direction - edge_direction + 180) % 360 - 180
                if (np.abs(angle1) > 90) & (np.abs(angle2) > 90):
                    edges_to_remove.append((u, v))
            except:
                edges_to_remove.append((u, v))
                
        # iterate over all edges
        for u, v in G_pruned.edges():
            # if edge meets pruning criterion, temporarily remove edge
            if G_pruned[u][v]['weight'] < min_passages:
                G_temp.remove_edge(u, v)
                # test if a path exists between u and v. When shorter than 5, add the edge to list of edges to remove
                if nx.has_path(G_temp, u, v):
                    if len(nx.shortest_path(G_temp, u, v)) <= 5:
                        if (u, v) not in edges_to_remove:
                            edges_to_remove.append((u, v))
    
        # Actually remove the edges after iterating through all of them
        for u, v in edges_to_remove:
            G_pruned.remove_edge(u, v)
            connections_pruned = connections_pruned[~((connections_pruned['from'] == u) & (connections_pruned['to'] == v))]

        # Output results
        print('------------------------')
        print(f'Pruned Graph:')
        print(f'Number of nodes: {G_pruned.number_of_nodes()} ({nx.number_of_isolates(G_pruned)} isolated)')
        print(f'Number of edges: {G_pruned.number_of_edges()}')
        print('------------------------')
        
        # Assign results
        self.G_pruned = G_pruned
        self.waypoint_connections_pruned = connections_pruned

    def refine_graph_from_paths(self, paths, trajectories):
        '''
        Refine the traffic graph based on a list of observed trajectories and their respective paths on the graph.
        The following actions are performed:
        - All edges that are not contained in the list of passages are pruned.
        - Edge features are re-computed
            * number of passages along each edge
            * speed distribution along each edge
            * cross-track distance distribution along each edge
        ====================================
        Params:
        paths: (Dataframe) observed paths mapped to the MTN
        trajectories: (MovingPandas TrajectoryCollection object) observed trajectories that were mapped to paths
        ====================================   
        '''
        start = time.time()  # start timer
        print(f'Refining graph based on {len(paths)} passages...')
        
        # initialize refined graph
        G_refined = self.G.copy()
        connections_refined = self.waypoint_connections.copy()
        connections_refined.reset_index(drop=True, inplace=True)
        waypoints = self.waypoints.copy()

        # initialize sparse matrices to capture the speed and distance distribution along each edge
        dim12 = np.max(self.waypoints.clusterID)+1
        dim3 = len(paths)
        speeds = sparse.DOK(shape=(dim12, dim12, dim3), dtype=np.float64)
        cross_dists = sparse.DOK(shape=(dim12, dim12, dim3, 10), dtype=np.float64)

        # set all edge weights that count passages to 0
        for u, v, data in G_refined.edges(data=True):
            data['weight'] = 0
            data['inverse_weight'] = 0
        connections_refined['passages'] = 0
        
        count = 0  # initialize a counter that keeps track of the progress
        percentage = 0  # percentage of progress
        print(f'Computing speeds and cross track distances for each trajectory:')
        print(f'Progress:', end=' ', flush=True)
        
        # add edge weight for each passage
        for i in range(0, len(paths)):
            mmsi = paths['mmsi'].iloc[i]
            path = paths['path'].iloc[i]
            trajectory = trajectories.get_trajectory(mmsi)
            for j  in range(0, len(path)-1):
                u = path[j]
                v = path[j+1]
                G_refined[u][v]['weight'] += 1  # add passage from u to v
                # compute speed along edge from u to v
                traj_segment, traj_points = geometry_utils.clip_trajectory_between_WPs(trajectory, u, v, waypoints)
                t1 = traj_points.index[0]  # get time at passage of waypoint u
                t2 = traj_points.index[-1]  # get time at passage of waypoint v
                length = traj_segment.length  # length of the segment between u and v
                if t2>t1:
                    time_diff = (t2-t1).total_seconds()  # time needed to get from u to v
                    speed = length / time_diff  # average speed between u and v
                else:
                    speed = 0
                speeds[u, v, i] = speed
                # compute distances between edge and trajectory segment
                edge = geometry_utils.node_sequence_to_linestring([u,v], connections_refined)
                # interpolate 10 points on trajectory segment to measure distance to the edge
                traj_points_interp = geometry_utils.interpolate_line_to_gdf(traj_segment, self.crs, n_points=9)
                traj_points_interp = traj_points_interp.head(10)
                #edge_points = geometry_utils.interpolate_line_to_gdf(edge, self.crs, n_points=len(traj_points))
                #sspd, _, _ = geometry_utils.sspd(traj_segment, traj_points, edge, edge_points)
                for k in range(len(traj_points_interp)):
                    dist = geometry_utils.signed_distance_to_line(edge, traj_points_interp.iloc[k].item())
                    #print(u, v, i, k)
                    #print(traj_points_interp.iloc[k].item(), dist)
                    cross_dists[u, v, i, k] = dist    # the k-th cross-track distance along the edge u,v observed from the  i-th path
            count += 1
            # report progress
            if count/len(paths) > 0.1:
                count = 0
                percentage += 10
                print(f'{percentage}%...', end='', flush=True)
        print('Done!')
        
        edges_to_remove = []  # placeholder for edges to remove
        
        print(f'Pruning edges and computing edge features...')
        # iterate over all edges
        for u, v in G_refined.edges():
            # if edge has not been sailed by any vessel, add it to the list of edges to remove
            if G_refined[u][v]['weight'] == 0:
                edges_to_remove.append((u, v))
            else:
                # update connections dataframe with number of passages
                ind = connections_refined[(connections_refined['from'] == u) & (connections_refined['to'] == v)].index
                connections_refined.loc[ind, 'passages'] = G_refined[u][v]['weight']
                
                # compute speed features along edge
                speeds_u_v = speeds[u, v, :].todense()
                speed_distribution = speeds_u_v[speeds_u_v > 0]
                speed_mean = np.mean(speed_distribution)
                speed_std = np.std(speed_distribution)
                speed_95_perc_confidence = [speed_mean+1.96*speed_std, speed_mean-1.96*speed_std]
                G_refined[u][v]['speed_mean'] = speed_mean
                G_refined[u][v]['speed_std'] = speed_std
                G_refined[u][v]['speed_95_perc_confidence'] = speed_95_perc_confidence
                G_refined[u][v]['speed_distribution'] = speed_distribution
                connections_refined.loc[ind, 'speed_mean'] = speed_mean
                connections_refined.loc[ind, 'speed_std'] = speed_std
                connections_refined.loc[ind, 'speed_95_perc_confidence'] = str(speed_95_perc_confidence)
                connections_refined.loc[ind, 'speed_distribution'] = str(speed_distribution.tolist())
                
                # update average cross track distance along edge
                cross_dists_u_v = cross_dists[u, v, :, :].todense().flatten()
                cross_dist_distribution = cross_dists_u_v[np.abs(cross_dists_u_v) > 0]  # eliminate 0 values
                cross_dist_mean = np.mean(cross_dist_distribution)
                cross_dist_std = np.std(cross_dist_distribution)
                cross_dist_skew = skew(cross_dist_distribution)
                cross_dist_kurtosis = kurtosis(cross_dist_distribution)
                cross_dist_95_perc_confidence = [cross_dist_mean+1.96*cross_dist_std, 
                                                 cross_dist_mean-1.96*cross_dist_std] # assuming normal distribution
                G_refined[u][v]['cross_track_dist_mean'] = cross_dist_mean
                G_refined[u][v]['cross_track_dist_std'] = cross_dist_std
                G_refined[u][v]['cross_track_dist_skew'] = cross_dist_skew
                G_refined[u][v]['cross_track_dist_kurtosis'] = cross_dist_kurtosis
                G_refined[u][v]['cross_track_dist_95_perc_confidence'] = cross_dist_95_perc_confidence
                G_refined[u][v]['cross_track_dist_distribution'] = cross_dist_distribution
                connections_refined.loc[ind, 'cross_track_dist_mean'] = cross_dist_mean
                connections_refined.loc[ind, 'cross_track_dist_std'] = cross_dist_std
                connections_refined.loc[ind, 'cross_track_dist_skew'] = cross_dist_skew
                connections_refined.loc[ind, 'cross_track_dist_kurtosis'] = cross_dist_kurtosis
                connections_refined.loc[ind, 'cross_track_dist_95_perc_confidence'] = str(cross_dist_95_perc_confidence)
                connections_refined.loc[ind, 'cross_track_dist_distribution'] = str(cross_dist_distribution.tolist())
                                     
        # Actually remove the edges after iterating through all of them
        for u, v in edges_to_remove:
            G_refined.remove_edge(u, v)
            connections_refined = connections_refined[~((connections_refined['from'] == u) & (connections_refined['to'] == v))]

        # Output results
        print('------------------------')
        print(f'Refined Graph:')
        print(f'Number of nodes: {G_refined.number_of_nodes()} / {self.G.number_of_nodes()} ({nx.number_of_isolates(G_refined)} / {nx.number_of_isolates(self.G)} isolated)')
        print(f'Number of edges: {G_refined.number_of_edges()} / {self.G.number_of_edges()}')
        print('------------------------')
        end = time.time()
        print(f'Time elapsed: {(end-start)/60:.2f} minutes')

        # Assign results
        connections_refined = gpd.GeoDataFrame(connections_refined, geometry='geometry', crs=self.crs)
        self.G_refined = G_refined
        self.waypoint_connections_refined = connections_refined

    def make_graph_from_waypoints(self, max_distance=10, max_angle=45):
        '''
        Connect computed waypoints based on observed trajectories.
        A connection is added between waypoints, when 
        - the trajectory has at most max_distance to the convex hulls of these waypoints and 
        - the angle between the waypoint traffic direction and the vessel's course is at most max_angle
        From waypoints and connections, we model a weighted, directed graph, where waypoints are nodes and connections are edges
        ====================================
        Params:
        max_distance: (float) maximum distance between a trajectory and waypoint convex hull
        max_angle: (float) maximum angle between vessel course and waypoint traffic direction
        ====================================
        result: self.waypoint_connections: A GeoDataframe holding the connections between waypoints as geometric objects
                self.G: The MTN represented as a networkx graph
        '''
        print(f'Constructing maritime traffic network graph from waypoints and trajectories...')
        print(f'Progress:', end=' ', flush=True)
        start = time.time()  # start timer
        
        # initialize graph adjacency matrix
        n_clusters = len(self.waypoints)
        coord_dict = {}  # this will temporarily store the adjacency matrix in COO format
        
        wps = self.waypoints.copy()
        wps.set_geometry('convex_hull', inplace=True)
        n_trajectories = len(self.significant_points.mmsi.unique())
        count = 0  # initialize a counter that keeps track of the progress
        percentage = 0  # percentage of progress
        
        # iterate over all trajectories
        for mmsi in self.significant_points.mmsi.unique():
            # find all intersections and close passages of waypoints
            trajectory = self.significant_points_trajectory.get_trajectory(mmsi)
            trajectory_segments = trajectory.to_line_gdf()
            distances = trajectory.distance(wps['convex_hull'])  # get distance of the trajectory to each waypoint convex hull
            mask = distances < max_distance
            close_wps = wps[mask]  # filter waypoints: Only those that are within max_distance of the trajectory
            # find temporal order  of waypoint passage
            passages = []  # initialize ordered list of waypoint passages per line segment
            # iterate over all trajectory segments
            for i in range(0, len(trajectory_segments)):
                segment = trajectory_segments.iloc[i]
                # distance of each segment to the selection of close waypoints
                distance_to_line = segment['geometry'].distance(close_wps['convex_hull'])  # distance between line segment and waypoint convex hull     
                distance_to_origin = segment['geometry'].boundary.geoms[0].distance(close_wps['geometry'])  # distance between first point of segment and waypoint centroids (needed for sorting)
                close_wps['distance_to_line'] = distance_to_line.tolist()
                close_wps['distance_to_origin'] = distance_to_origin.tolist()
                
                # angle between line segment and mean traffic direction in each waypoint
                WP_cog_before = close_wps['cog_before'] 
                WP_cog_after  = close_wps['cog_after']
                trajectory_cog = segment['direction']
                close_wps['angle_before'] = np.abs(WP_cog_before - trajectory_cog + 180) % 360 - 180
                close_wps['angle_after'] = np.abs(WP_cog_after - trajectory_cog + 180) % 360 - 180
                # the line segment is associated with the waypoint, when its distance and angle is less than a threshold
                mask = ((close_wps['distance_to_line']<max_distance) & 
                        (np.abs(close_wps['angle_before'])<max_angle) & 
                        (np.abs(close_wps['angle_after'])<max_angle))
                passed_wps = close_wps[mask]
                passed_wps.sort_values(by='distance_to_origin', inplace=True)
                passages.extend(passed_wps['clusterID'].tolist())
            passages = list(OrderedDict.fromkeys(passages))
            
            # create edges between subsequent passed waypoints
            if len(passages) > 1:  # subset needs to contain at least 2 waypoints
                for i in range(0, len(passages)-1):
                    row = passages[i]
                    col = passages[i+1]
                    if row != col:  # no self loops
                        if (row, col) in coord_dict:
                            coord_dict[(row, col)] += 1  # increase the edge weight for each passage
                        else:
                            coord_dict[(row, col)] = 1  # create edge if it does not exist yet
            
            count += 1
            # report progress
            if count/n_trajectories > 0.1:
                count = 0
                percentage += 10
                print(f'{percentage}%...', end='', flush=True)

        # store adjacency matrix as sparse matrix in COO format
        row_indices, col_indices = zip(*coord_dict.keys())
        values = list(coord_dict.values())
        A = coo_matrix((values, (row_indices, col_indices)), shape=(n_clusters, n_clusters))

        # initialize directed graph from adjacency matrix
        G = nx.from_scipy_sparse_array(A, create_using=nx.DiGraph)

        # Add node features
        for i in range(0, len(self.waypoints)):
            node_id = self.waypoints.clusterID.iloc[i]
            G.nodes[node_id]['n_members'] = self.waypoints.n_members.iloc[i]
            G.nodes[node_id]['position'] = (self.waypoints.lon.iloc[i], self.waypoints.lat.iloc[i])  # !changed lat-lon to lon-lat for plotting
            G.nodes[node_id]['speed'] = self.waypoints.speed.iloc[i]
            G.nodes[node_id]['cog_before'] = self.waypoints.cog_before.iloc[i]
            G.nodes[node_id]['cog_after'] = self.waypoints.cog_after.iloc[i]
        
        # Construct a GeoDataFrame from graph edges
        waypoints = self.waypoints.copy()
        waypoints.set_geometry('geometry', inplace=True, crs=self.crs)
        waypoint_connections = pd.DataFrame(columns=['from', 'to', 'geometry', 'direction', 'length', 'passages'])
        for orig, dest, weight in zip(A.row, A.col, A.data):
            # add linestring as edge
            p1 = waypoints[waypoints.clusterID == orig]['geometry']
            p2 = waypoints[waypoints.clusterID == dest]['geometry']
            edge = LineString([(p1.x, p1.y), (p2.x, p2.y)])
            length = edge.length
            # compute the orientation fo the edge (COG)
            p1 = Point(waypoints[waypoints.clusterID == orig].lon, waypoints[waypoints.clusterID == orig].lat)
            p2 = Point(waypoints[waypoints.clusterID == dest].lon, waypoints[waypoints.clusterID == dest].lat)
            direction = geometry_utils.calculate_initial_compass_bearing(p1, p2)
            line = pd.DataFrame([[orig, dest, edge, direction, length, weight]], 
                                columns=['from', 'to', 'geometry', 'direction', 'length', 'passages'])
            waypoint_connections = pd.concat([waypoint_connections, line])
        waypoint_connections = gpd.GeoDataFrame(waypoint_connections, geometry='geometry', crs=self.crs)

        # Add edge features
        for i in range(0, len(waypoint_connections)):
            orig = waypoint_connections['from'].iloc[i]
            dest = waypoint_connections['to'].iloc[i]
            G[orig][dest]['length'] = waypoint_connections['length'].iloc[i]
            G[orig][dest]['direction'] = waypoint_connections['direction'].iloc[i]
            G[orig][dest]['geometry'] = waypoint_connections['geometry'].iloc[i]
            G[orig][dest]['inverse_weight'] = 1/waypoint_connections['passages'].iloc[i]
       
        # Output results
        print('Done!')
        print('------------------------')
        print(f'Unpruned Graph:')
        print(f'Number of nodes: {G.number_of_nodes()} ({nx.number_of_isolates(G)} isolated)')
        print(f'Number of edges: {G.number_of_edges()}')
        print(f'Network is (weakly) connected: {nx.is_weakly_connected(G)}')
        print('------------------------')
        
        # Assign results
        self.G = G
        self.waypoint_connections = gpd.GeoDataFrame(waypoint_connections, geometry='geometry', crs=self.crs)
        
        end = time.time()
        print(f'Time elapsed: {(end-start)/60:.2f} minutes')      

    def trajectory_to_path_sspd(self, trajectory, k_max=500, l_max=5, algorithm='standard', verbose=False):
        '''
        A fast map-matching algorithm to map trajectories to paths.
        The algorithm maps a trajectory to a path on the graph, trying to minimize the SSPD between trajectory and path according to the following steps:
        1. Find suitable waypoint close to the origin of the trajectory
        2. Find suitable waypoint close to the destination of the trajectory
        3. Find all waypoints passed by the trajectory
        4. Compute the edge sequence between each pair of passed waypoints that minimizes the SSPD to the trajectory
        ====================================
        Params:
        trajectory: (MovingPandas Trajectory object) a trajectory
        k_max: (int) algorithm hyperparameter for maximum subpath alternatives
        l_max: (int) algorithm hyperparameter for maximum subpath length
        algorithm: (str) 'standard' or 'refined' ('refined' hops over some intermediate waypoints that were visited, but increase the SSPD)
        ====================================
        Returns
        path_df: (GeoDataframe) contains the segments of the mapped path row by row 
        evaluation_results: (Dataframe) contains evaluation metrics about the mapped path with respect to the original trajectory
        '''
        # initialize variables
        G = self.G_pruned.copy()
        waypoints = self.waypoints.copy()
        connections = self.waypoint_connections_pruned.copy()
        points = trajectory.to_point_gdf()
        mmsi = points.mmsi.unique()[0]
        
        #print(mmsi)
        if verbose: 
            print('=======================')
            print(mmsi)
            print('=======================')
        
        try:
            ### GET potential START POINT ###
            orig_WP, idx_orig, dist_orig = geometry_utils.find_orig_WP(points, waypoints)
            ### GET potential END POINT ###
            dest_WP, idx_dest, dist_dest = geometry_utils.find_dest_WP(points, waypoints)
            if verbose: print('Potential start and end point:', orig_WP, dest_WP)
        except:
            orig_WP = 0
            dest_WP = 0
            dist_orig = np.inf
            dist_dest = np.inf

        # Get all intersections between trajectory and waypoints 
        # as well as subgraph in a channel around that trajectory
        passages, G_channel = geometry_utils.find_WP_intersections(points, trajectory, waypoints, G, 1000)
        if verbose: print('Intersections found:', passages)
        
        # Distinguish three cases
        # 1. passages is empty and orig != dest
        if ((len(passages) == 0) & (orig_WP != dest_WP)):
            if dist_orig < 100:
                passages.append(orig_WP)
            if dist_dest < 100:
                passages.append(dest_WP)
        # 2. passages is empty and orig == dest --> nothing we can do here
        elif ((len(passages) == 0) & (orig_WP == dest_WP)):
            passages = []
        # 3. found passages
        else:
            # if the potential start waypoint is not in the list of intersections, but close to the origin of the trajectory, add it to the set of passages
            if ((orig_WP not in passages) & (dist_orig < 100) & (nx.has_path(G, orig_WP, passages[0]))):
                passages.insert(0, orig_WP)
            else:
                orig_WP = passages[0]
                orig_WP_point = waypoints[waypoints.clusterID==orig_WP]['geometry'].item()
                idx_orig = np.argmin(orig_WP_point.distance(points.geometry))
            
            # if the potential destination waypoint is not in the list of intersections, but close to the destination of the trajectory, add it to the set of passages
            if ((dest_WP not in passages) & (dist_dest < 100) & (nx.has_path(G, passages[-1], dest_WP))):
                passages.append(dest_WP)
            else:
                dest_WP = passages[-1]
                dest_WP_point = waypoints[waypoints.clusterID==dest_WP]['geometry'].item()
                idx_dest = np.argmin(dest_WP_point.distance(points.geometry))
        if verbose: print('Intersections with start and endpoint:', passages)
        
        if len(passages) >= 2:
            try:
                if verbose: print('Executing try statement')
                path = [[passages[0]]] # initialize the best edge sequence traversed by the vessel
                #eval_distances = []  # initialize list for distances between trajectory and edge sequence
                # find the edge sequence between each waypoint pair, that MINIMIZES THE DISTANCE between trajectory and edge sequence
                for i in range(0, len(passages)-1):
                    if verbose: print('From:', passages[i], ' To:', passages[i+1])
                    WP1 = waypoints[waypoints.clusterID==passages[i]]['geometry'].item()  # coordinates of waypoint at beginning of edge sequence
                    WP2 = waypoints[waypoints.clusterID==passages[i+1]]['geometry'].item()  # coordinates of waypoint at end of edge sequence
                    idx1 = np.argmin(WP1.distance(points.geometry))  # index of trajectory point closest to beginning of edge sequence
                    idx2 = np.argmin(WP2.distance(points.geometry))  # index of trajectory point closest to end of edge sequence
                    if verbose: print('Point indices:', idx1, idx2)
                    # Check if we are moving backwards
                    if idx2 < idx1:
                        if verbose: print('going back is not allowed! (inner)')
                        continue
                    ### CORE FUNCTION
                    ### when current waypoint pair is very close, just take the shortest path to save computation time
                    if (idx2-idx1) <= 3:
                        if verbose: print('Close waypoints. Taking shortest path')
                        edge_sequences = nx.all_shortest_paths(G_channel, passages[i], passages[i+1])
                    # if waypoints are further apart, explore longer paths
                    else:
                        # compute length of shortest possible path
                        min_sequence_length = len(nx.shortest_path(G_channel, passages[i], passages[i+1]))
                        # if the shortest path is already long, just take the shortest path
                        if min_sequence_length > l_max:
                            if verbose: print('Far apart waypoints. Taking shortest path.')
                            edge_sequences = nx.all_shortest_paths(G_channel, passages[i], passages[i+1])
                        # if the shortest path is short, explore alternative paths
                        else:
                            if verbose: print('Far apart waypoints. Exploring all paths with limited length.')
                            cutoff = l_max
                            edge_sequences = nx.all_simple_paths(G_channel, passages[i], passages[i+1], cutoff=cutoff)
                            while len(list(edge_sequences)) > k_max:
                                cutoff -= 1
                                if verbose: print('Too many alternative paths. Reducing cutoff to ', cutoff)
                                edge_sequences = nx.all_simple_paths(G_channel, passages[i], passages[i+1], cutoff=cutoff)
                            if verbose: print('Final cutoff ', cutoff)
                            edge_sequences = nx.all_simple_paths(G_channel, passages[i], passages[i+1], cutoff=cutoff)    
                    #################
                    if verbose: print('=======================')
                    if verbose: print(f'Iterating through edge sequences')
                    min_mean_distance = np.inf
                    # iterate over all possible connections
                    for edge_sequence in edge_sequences:
                        # create a linestring from the edge sequence
                        if verbose: print('Edge sequence:', edge_sequence)
                        multi_line = []
                        for j in range(0, len(edge_sequence)-1):
                            line = connections[(connections['from'] == edge_sequence[j]) & (connections['to'] == edge_sequence[j+1])].geometry.item()
                            multi_line.append(line)
                        multi_line = MultiLineString(multi_line)
                        multi_line = ops.linemerge(multi_line)  # merge edge sequence to a single linestring
                        # measure distance between the multi_line and the trajectory
                        if idx2 == idx1:
                            eval_points = points.iloc[idx1]  # trajectory points associated with the edge sequence
                            eval_point = eval_points['geometry']
                            SSPD = eval_point.distance(multi_line)
                        else:
                            # get the SSPD between trajectory and edge sequence
                            eval_points = points.iloc[idx1:idx2+1]  # trajectory points associated with the edge sequence
                            t1 = points.index[idx1]
                            t2 = points.index[idx2]
                            eval_traj = trajectory.get_linestring_between(t1, t2)  # trajectory associated with the edge sequence
                            num_points = len(eval_points)
                            interpolated_points = [multi_line.interpolate(dist) for dist in range(0, int(multi_line.length)+1, int(multi_line.length/num_points)+1)]
                            interpolated_points_coords = [Point(point.x, point.y) for point in interpolated_points]  # interpolated points on edge sequence
                            interpolated_points = pd.DataFrame({'geometry': interpolated_points_coords})
                            interpolated_points = gpd.GeoDataFrame(interpolated_points, geometry='geometry', crs=self.crs)
                            SSPD, d12, d21 = geometry_utils.sspd(eval_traj, eval_points['geometry'], multi_line, interpolated_points['geometry'])
                            # punish longer edge sequences
                            SSPD = SSPD * np.max([multi_line.length/eval_traj.length, 1.0])
                            if verbose: print('   SSPD:', SSPD)
                        # when mean distance is smaller than any mean distance encountered before --> save current edge sequence as best edge sequence
                        if SSPD < min_mean_distance:
                            min_mean_distance = SSPD
                            best_sequence = edge_sequence
                    path.append(best_sequence[1:])
                    if verbose: print('----------------------')
                # flatten path
                path = [item for sublist in path for item in sublist]
                #eval_distances = [item for sublist in eval_distances for item in sublist]
                message = 'success'
                if verbose: print('Found path:', path)
                if verbose: print(mmsi, nx.is_path(G, path))

                if algorithm=='refined':
                    ## refine path ##
                    refined_path = [path[0]]
                    skipped=False
                    for k in range(0, len(path)-2):
                        if skipped:
                            skipped=False
                            continue
                        WP1_id = path[k]
                        WP2_id = path[k+1]
                        WP3_id = path[k+2]
                        if geometry_utils.is_valid_path(G_channel, [WP1_id, WP3_id]):
                            traj12, traj12p = geometry_utils.clip_trajectory_between_WPs(trajectory, WP1_id, WP2_id, waypoints)
                            traj23, traj23p = geometry_utils.clip_trajectory_between_WPs(trajectory, WP2_id, WP3_id, waypoints)
                            traj13, traj13p = geometry_utils.clip_trajectory_between_WPs(trajectory, WP1_id, WP3_id, waypoints)
                            edge12 = geometry_utils.node_sequence_to_linestring([WP1_id, WP2_id], connections)
                            edge23 = geometry_utils.node_sequence_to_linestring([WP2_id, WP3_id], connections)
                            edge13 = geometry_utils.node_sequence_to_linestring([WP1_id, WP3_id], connections)
                            edge12p = geometry_utils.interpolate_line_to_gdf(edge12, self.crs, n_points=len(traj12p))
                            edge23p = geometry_utils.interpolate_line_to_gdf(edge23, self.crs, n_points=len(traj23p))
                            edge13p = geometry_utils.interpolate_line_to_gdf(edge13, self.crs, n_points=len(traj13p))
                            SSPD12, d12, d21 = geometry_utils.sspd(traj12, traj12p, edge12, edge12p)
                            SSPD23, d23, d32 = geometry_utils.sspd(traj23, traj23p, edge23, edge23p)
                            SSPD13, d13, d31 = geometry_utils.sspd(traj13, traj13p, edge13, edge13p)
                            f123 = np.mean(d12.tolist() + d21.tolist() + d23.tolist() + d32.tolist())
                            f13 = np.mean(d13.tolist() + d31.tolist())
                            len123 = edge12.length + edge23.length
                            len13 = edge13.length
                            if  f123*len123 > f13*len13:
                                refined_path.append(WP3_id)
                                skipped=True
                            else:
                                refined_path.append(WP2_id)
                        else:
                            refined_path.append(WP2_id)
                    if refined_path[-1] != path[-1]:
                        refined_path.append(path[-1])
                    path = refined_path
                
        
                # Compute GeoDataFrame from path, containing the edge sequence as LineStrings
                path_df = pd.DataFrame(columns=['mmsi', 'orig', 'dest', 'geometry', 'message'])
                for j in range(0, len(path)-1):
                    #print(path[j], path[j+1])
                    edge = connections[(connections['from'] == path[j]) & (connections['to'] == path[j+1])].geometry.item()
                    temp = pd.DataFrame([[mmsi, path[j], path[j+1], edge, message]], columns=['mmsi', 'orig', 'dest', 'geometry', 'message'])
                    path_df = pd.concat([path_df, temp])
                path_df = gpd.GeoDataFrame(path_df, geometry='geometry', crs=self.crs)
        
                ###########
                # evaluate goodness of fit
                ###########
                if idx_orig >= idx_dest:  # In some cases this is needed, for example for roundtrips of ferries
                    idx_orig = 0
                    idx_dest = -1
                eval_points = points.iloc[idx_orig:idx_dest]  # the subset of points we are evaluating against
                multi_line = MultiLineString(list(path_df['geometry']))
                edge_sequence = ops.linemerge(multi_line)  # merge edge sequence to a single linestring
                # compute the fraction of trajectory that can be associated with an edge sequence
                t1 = points.index[idx_orig]
                t2 = points.index[idx_dest]
                try:
                    eval_traj = trajectory.get_linestring_between(t1, t2)
                    percentage_covered = eval_traj.length / trajectory.get_length()
                except:
                    eval_traj = trajectory
                    percentage_covered = 1
                num_points = len(eval_points)
                interpolated_points = [edge_sequence.interpolate(dist) for dist in range(0, int(edge_sequence.length)+1, int(edge_sequence.length/num_points)+1)]
                interpolated_points_coords = [Point(point.x, point.y) for point in interpolated_points]  # interpolated points on edge sequence
                interpolated_points = pd.DataFrame({'geometry': interpolated_points_coords})
                interpolated_points = gpd.GeoDataFrame(interpolated_points, geometry='geometry', crs=self.crs)    
                SSPD, d12, d21 = geometry_utils.sspd(eval_traj, eval_points['geometry'], edge_sequence, interpolated_points['geometry'])
                distances = d12.tolist() + d21.tolist()
                evaluation_results = pd.DataFrame({'mmsi':mmsi,
                                                   'SSPD':SSPD,
                                                   'distances':[distances],
                                                   'fraction_covered':percentage_covered,
                                                   'message':message,
                                                   'path':[path],
                                                   'path_linestring':edge_sequence}
                                                 )
               
            # In some cases the above algorithm gets stuck in waypoints without any connections leading to the next waypoint
            # In this case we attempt to find the shortest path between origin and destination
            except:
                if verbose: print('Executing except statement...')
                # if a path exists, we compute it
                if nx.has_path(G, passages[0], passages[-1]):
                    path = nx.shortest_path(G, passages[0], passages[-1])
                    message = 'attempt'
                    # Compute GeoDataFrame from path, containing the edge sequence as LineStrings
                    path_df = pd.DataFrame(columns=['mmsi', 'orig', 'dest', 'geometry', 'message'])
                    for j in range(0, len(path)-1):
                        edge = connections[(connections['from'] == path[j]) & (connections['to'] == path[j+1])].geometry.item()
                        temp = pd.DataFrame([[mmsi, path[j], path[j+1], edge, message]], columns=['mmsi', 'orig', 'dest', 'geometry', 'message'])
                        path_df = pd.concat([path_df, temp])
                    path_df = gpd.GeoDataFrame(path_df, geometry='geometry', crs=self.crs)
            
                    ###########
                    # evaluate goodness of fit
                    ###########
                    if idx_orig >= idx_dest:  # In some cases this is needed, for example for roundtrips of ferries
                        idx_orig = 0
                        idx_dest = -1
                    eval_points = points.iloc[idx_orig:idx_dest]  # the subset of points we are evaluating against
                    multi_line = MultiLineString(list(path_df.geometry))
                    edge_sequence = ops.linemerge(multi_line)  # merge edge sequence to a single linestring
                    t1 = points.index[idx_orig]
                    t2 = points.index[idx_dest]
                    try:
                        eval_traj = trajectory.get_linestring_between(t1, t2)
                        percentage_covered = eval_traj.length / trajectory.get_length()
                    except:
                        eval_traj = trajectory
                        percentage_covered = 1
                    num_points = len(eval_points)
                    interpolated_points = [edge_sequence.interpolate(dist) for dist in range(0, int(edge_sequence.length)+1, int(edge_sequence.length/num_points)+1)]
                    interpolated_points_coords = [Point(point.x, point.y) for point in interpolated_points]  # interpolated points on edge sequence
                    interpolated_points = pd.DataFrame({'geometry': interpolated_points_coords})
                    interpolated_points = gpd.GeoDataFrame(interpolated_points, geometry='geometry', crs=self.crs)    
                    SSPD, d12, d21 = geometry_utils.sspd(eval_traj, eval_points['geometry'], edge_sequence, interpolated_points['geometry'])
                    distances = d12.tolist() + d21.tolist()
                    evaluation_results = pd.DataFrame({'mmsi':mmsi,
                                                       'SSPD':SSPD,
                                                       'distances':[distances],
                                                       'fraction_covered':percentage_covered,
                                                       'message':message,
                                                       'path':[path],
                                                       'path_linestring':edge_sequence}
                                                     )
                # If there is no path between origin and destination, we cannot map the trajectory to an edge sequence
                else:
                    message = 'no_path'
                    path_df = pd.DataFrame({'mmsi':mmsi, 'orig':orig_WP, 'dest':dest_WP, 'geometry':[], 'message':message})
                    evaluation_results = pd.DataFrame({'mmsi':mmsi,
                                                       'SSPD':np.nan,
                                                       'distances':[np.nan],
                                                       'fraction_covered':0,
                                                       'message':message,
                                                       'path':[np.nan],
                                                       'path_linestring':np.nan}
                                                     )
        
        # When there are no intersections with any waypoints, we cannot map the trajectory to an edge sequence
        else:
            #print('Not enough intersections found. Cannot map trajectory to graph...')
            message = 'no_intersects'
            #print(mmsi, ': failure')
            path_df = pd.DataFrame({'mmsi':mmsi, 'orig':orig_WP, 'dest':dest_WP, 'geometry':[], 'message':message})
            evaluation_results = pd.DataFrame({'mmsi':mmsi,
                                               'SSPD':np.nan,
                                               'distances':[np.nan],
                                               'fraction_covered':0,
                                               'message':message,
                                               'path':[np.nan],
                                               'path_linestring':np.nan}
                                             )
        return path_df, evaluation_results
        
    def dijkstra_shortest_path(self, orig, dest, weight='inverse_weight'):
        '''
        Finds the shortest path in the network using Dijkstra's algorithm.
        ====================================
        Params:
        orig: (int) ID of the start waypoint
        dest: (int) ID of the destination waypoint
        weight: (str) name of the edge feature to be used as weight in Dijkstra's algorithm
        ====================================   
        Returns:
        dijkstra_path_df: (GeoDataframe) the shortest path between orig and dest
        '''     
        try:
            # compute shortest path using dijsktra's algorithm (outputs a list of nodes)
            shortest_path = nx.dijkstra_path(self.G_pruned, orig, dest, weight=weight)
        except:
            print(f'Nodes {orig} and {dest} are not connected. Exiting...')
            return False
        else:
            # generate plottable GeoDataFrame from dijkstra path
            dijkstra_path_df = pd.DataFrame(columns=['orig', 'dest', 'geometry'])
            connections = self.waypoint_connections_pruned.copy()
            for j in range(0, len(shortest_path)-1):
                edge = connections[(connections['from'] == shortest_path[j]) & (connections['to'] == shortest_path[j+1])].geometry.item()
                temp = pd.DataFrame([[shortest_path[j], shortest_path[j+1], edge]], columns=['orig', 'dest', 'geometry'])
                dijkstra_path_df = pd.concat([dijkstra_path_df, temp])
            dijkstra_path_df = gpd.GeoDataFrame(dijkstra_path_df, geometry='geometry', crs=self.crs)
            return dijkstra_path_df
    
    def evaluate_graph(self, trajectories, k_max=500, l_max=5, algorithm='standard'):
        '''
        Given a selection of trajectories, compute and visualize evaluation metrics for the Maritime Traffic Network
        ====================================
        Params:
        trajectories: (MovingPandas TrajectoryCollection) trajectories to evaluate the MTN against
        k_max: (int) algorithm hyperparameter for maximum subpath alternatives (for trajectory_to_path_sspd algorithm)
        l_max: (int) algorithm hyperparameter for maximum subpath length (for trajectory_to_path_sspd algorithm)
        algorithm: (str) 'standard' (uses trajectory_to_path_sspd map matching algorithm) 
                         'refined' (uses trajectory_to_path_sspd map matching algorithm;'refined' hops over some intermediate waypoints that were visited, but increase the SSPD) 
                         'leuven' (uses leuven map_matching algorithm)
        ====================================   
        Returns:
        all_paths: (GeoDataframe) all map-matched paths
        all_evaluation_results: (Dataframe) evaluation metrics
        summary: (dict) summary of evaluation metrics
        fig: (matplotlib figure) plot of evaluation metrics
        '''
        # initialize output variables
        all_paths = pd.DataFrame(columns=['mmsi', 'orig', 'dest', 'geometry', 'message'])
        #all_evaluation_results = pd.DataFrame(columns = ['mmsi', 'mean_dist', 'median_dist', 'max_dist', 'distances', 'message'])
        all_evaluation_results = pd.DataFrame(columns = ['mmsi', 'SSPD', 'distances', 'fraction_covered', 'message', 'path','path_linestring'])
        n_trajectories = len(trajectories)
            
        print(f'Evaluating graph on {n_trajectories} trajectories')
        print(f'Progress:', end=' ', flush=True)
        count = 0  # initialize a counter that keeps track of the progress
        percentage = 0  # percentage of progress
        # iterate over all evaluation trajectories
        start = time.time()
        for trajectory in trajectories:
            if algorithm == 'leuven':
                path, evaluation_results = self.map_matching(trajectory)  # evaluate trajectory
            else:
                path, evaluation_results = self.trajectory_to_path_sspd(trajectory, k_max, l_max, algorithm)  # evaluate trajectory
            all_paths = pd.concat([all_paths, path])
            all_evaluation_results = pd.concat([all_evaluation_results, evaluation_results])
            count += 1
            # report progress
            if count/n_trajectories > 0.1:
                count = 0
                percentage += 10
                print(f'{percentage}%...', end='', flush=True)
        end = time.time()
        print(f'Done! Time elapsed for evaluation: {(end-start)/60:.2f} minutes')

        # get percentages of success / attempt / orig_is_dest
        print('Success rates:')
        print((all_evaluation_results.groupby('message').count() / len(all_evaluation_results)).to_string())
        print('\n --------------------------- \n')
        
        # percentage of trajectories that could not be mapped to the graph
        nan_mask = all_evaluation_results.isna().any(axis=1)
        num_rows_with_nan = all_evaluation_results[nan_mask].shape[0]
        percentage_nan = num_rows_with_nan/len(all_evaluation_results)
        print(f'Fraction of NaN results: {percentage_nan:.3f}')
        print('\n --------------------------- \n')
        mean_fraction_covered = all_evaluation_results[~nan_mask]['fraction_covered'].mean()
        print(f'Mean fraction of each trajectory covered by the path on the graph: {mean_fraction_covered:.3f} \n')
        
        #not_nan = all_evaluation_results[~nan_mask]
        success_mask = all_evaluation_results['message']=='success'
        success_eval_results = all_evaluation_results[success_mask]
        distances = success_eval_results['distances'].tolist()
        distances = [item for sublist in distances for item in sublist]
        
        print(f'Mean distance      = {np.mean(distances):.2f} m')
        print(f'Median distance    = {np.median(distances):.2f} m')
        print(f'Standard deviation = {np.std(distances):.2f} m \n')

        # assuming a half normal distribution of distances, compute distribution parameters
        dist_array = np.array(distances)
        loc, scale = halfnorm.fit(dist_array, floc=0)  # scale is the standard deviation of the underlying normal distribution
        mean_hnd = halfnorm.mean(loc=0, scale=scale)
        std_hnd = halfnorm.std(loc=0, scale=scale)
        
        # Plot results
        fig, axes = plt.subplots(1, 3, figsize=(12, 6))

        # Plot 1: Distance between each trajectory and edge sequence
        plot_evaluation = success_eval_results
        axes[0].boxplot(plot_evaluation.distances)
        axes[0].set_title('Distance between trajectory \n and edge sequence')
        axes[0].set_ylabel('Distance (m)')
        axes[0].set_xlabel(f'{n_trajectories} trajectories')
        axes[0].set_xticks([])

        # Plot 2: Distance between all trajectories and edge sequences (outlier cutoff at 2000m)
        axes[1].boxplot(distances)
        axes[1].set_title('Distance between all trajectories \n and edge sequences \n (outlier cutoff at 2000m)')
        axes[1].set_ylabel('Distance (m)')
        axes[1].set_ylim([0, 500])

        # Plot 3: Distribution of distance
        axes[2].hist(distances, bins=np.arange(0, 2000, 20).tolist())
        axes[2].set_title('Distribution of distance')
        axes[2].set_xlabel('Distance (m)')
        
        # Adjust layout
        plt.tight_layout()
        # Show the plot
        plt.show()
        
        summary = {'Mean distance':np.mean(distances),
                   'Median distance':np.median(distances),
                   'Standard deviation':np.std(distances),
                   'HND_scale':scale,
                   'HND_mean':mean_hnd,
                   'HND_std':std_hnd,
                   'Fraction NaN':percentage_nan,
                   'Fraction success':len(all_evaluation_results[all_evaluation_results.message=='success']) / len(all_evaluation_results),
                   'mean fraction covered': mean_fraction_covered}

        all_paths = gpd.GeoDataFrame(all_paths, geometry='geometry', crs=self.crs)
        return all_paths, all_evaluation_results, summary, fig
    
    def map_waypoints(self, detailed_plot=False, center=[59, 5], opacity=1):
        '''
        Method for the visualization of waypoints on a folium map object
        Visualizes waypoints categorized by traffic direction and speed:
        - Blue: Stop points
        - Green: Waypoints with eastbound traffic direction
        - Red: Waypoints with westbound traffic direction
        ====================================
        Params:
        detailed_plot: (bool) if True, all trajectories and significant points are added to the map as vector data (can crash for too many trajectories)
                              if False, a hexagonally binned plot of the actual traffic density is added as a raster overlay (recommended)
        center: [float, float] center of the map in lon lat coordinates
        opacity: (float) opacity of the displayed waypoints
        ====================================   
        Returns:
        map: (Folium map object) map with waypoints as layers
        '''
        # plotting
        if detailed_plot:
            columns = ['geometry', 'mmsi']  # columns to be plotted
            # plot trajectories
            map = self.trajectories.to_traj_gdf()[columns].explore(column='mmsi', name='Trajectories', 
                                                                      style_kwds={'weight':1, 'color':'black', 'opacity':0.5}, 
                                                                      legend=False)
            # plot significant turning points with their cluster ID
            map = self.significant_points[['clusterID', 'geometry']].explore(m=map, name='all waypoints with cluster ID', 
                                                                                legend=False,
                                                                                marker_kwds={'radius':2},
                                                                                style_kwds={'opacity':0.2})
        else:
            # plot basemap
            map = folium.Map(location=center, tiles="OpenStreetMap", zoom_start=8)
            # plot traffic as raster overlay
            map = visualize.traffic_raster_overlay(self.gdf.to_crs(4326), map)
        
        # plot cluster centroids and their convex hull
        cluster_centroids = self.waypoints.copy()
        cluster_centroids.to_crs(4326, inplace=True)
        columns_points = ['clusterID', 'geometry', 'speed', 'cog_before', 'cog_after', 'n_members']  # columns to plot
        columns_hull = ['clusterID', 'convex_hull', 'speed', 'cog_before', 'cog_after', 'n_members']  # columns to plot
        
        # plot eastbound cluster centroids
        eastbound = cluster_centroids[(cluster_centroids.cog_after < 180) & (cluster_centroids.speed >= 2.0)]
        eastbound.set_geometry('geometry', inplace=True)
        map = eastbound[columns_points].explore(m=map, name='cluster centroids (eastbound)', legend=False,
                                                marker_kwds={'radius':3},
                                                style_kwds={'color':'green', 'fillColor':'green', 'fillOpacity':opacity, 'opacity':opacity})
        eastbound.set_geometry('convex_hull', inplace=True, crs=self.crs)
        map = eastbound[columns_hull].explore(m=map, name='cluster convex hulls (eastbound)', legend=False,
                                              style_kwds={'color':'green', 'fillColor':'green', 'fillOpacity':0.2, 'opacity':opacity})
        
        # plot westbound cluster centroids
        westbound = cluster_centroids[(cluster_centroids.cog_after >= 180) & (cluster_centroids.speed >= 2.0)]
        westbound.set_geometry('geometry', inplace=True)
        map = westbound[columns_points].explore(m=map, name='cluster centroids (westbound)', legend=False,
                                                marker_kwds={'radius':3},
                                                style_kwds={'color':'red', 'fillColor':'red', 'fillOpacity':opacity, 'opacity':opacity})
        westbound.set_geometry('convex_hull', inplace=True, crs=self.crs)
        map = westbound[columns_hull].explore(m=map, name='cluster convex hulls (westbound)', legend=False,
                                              style_kwds={'color':'red', 'fillColor':'red', 'fillOpacity':0.2, 'opacity':opacity})
        
        # plot stop cluster centroids
        stops = cluster_centroids[cluster_centroids.speed < 2.0]
        if len(stops) > 0:
            stops.set_geometry('geometry', inplace=True)
            map = stops[columns_points].explore(m=map, name='cluster centroids (stops)', legend=False,
                                                marker_kwds={'radius':3},
                                                style_kwds={'color':'blue', 'fillColor':'blue', 'fillOpacity':opacity, 'opacity':opacity})
            stops.set_geometry('convex_hull', inplace=True, crs=self.crs)
            map = stops[columns_hull].explore(m=map, name='cluster convex hulls (stops)', legend=False,
                                              style_kwds={'color':'blue', 'fillColor':'blue', 'fillOpacity':0.2, 'opacity':opacity})
        #folium.LayerControl().add_to(map)

        return map

    def map_graph(self, pruned=False, refined=False, location='stavanger', min_passages=1, line_weight=1, opacity=1):
        '''
        Method for the visualization of the Maritime Traffic Network on a folium map object
        ====================================
        Params:
        pruned: (bool) if True, the pruned MTN is visualized
        refined: (bool) if True, the refined MTN is visualized
        location: (str) the geographic region to center the map around (supported: 'stavanger', 'tromso')
        min_passages: (int) only edges a minimium of min_passages will be displayed
        line_weight: (float) line weight for edge display
        opacity: (float) opacity of the displayed waypoints and edges
        ====================================   
        Returns:
        map: (Folium map object) map with the MTN as layers
        '''
        if location=='stavanger':
            center=[59, 5]
        elif location=='tromso':
            center=[69, 19]
        else:
            center=[59, 10.5]
        # basemap with waypoints
        map = self.map_waypoints(detailed_plot=False, center=center, opacity=opacity)

        # add connections
        if pruned:
            connections = self.waypoint_connections_pruned.copy()
        elif refined:
            connections = self.waypoint_connections_refined.copy()
            connections = connections.drop(['speed_distribution', 'cross_track_dist_distribution'], axis=1)
        else:
            connections = self.waypoint_connections.copy()
        connections = connections[connections['passages'] >= min_passages]
        
        # Plot connections
        eastbound = connections[(connections.direction < 180)]
        westbound = connections[(connections.direction >= 180)]
        map = westbound.explore(m=map, name='westbound graph edges', legend=False,
                                style_kwds={'weight':line_weight, 'color':'red', 'opacity':opacity})
        map = eastbound.explore(m=map, name='eastbound graph edges', legend=False,
                                style_kwds={'weight':line_weight, 'color':'green', 'opacity':opacity})
        return map
    
    def plot_graph_canvas(self, pruned=False, refined=False):
        '''
        Plot the maritime traffic network graph on a white canvas
        ====================================
        Params:
        pruned: (bool) if True, the pruned MTN is visualized
        refined: (bool) if True, the refined MTN is visualized
        ====================================   
        Returns:
        '''
        if pruned == True:
            G = self.G_pruned
        elif refined == True:
            G = self.G_refined
        else:
            G = self.G
        # Create a dictionary mapping nodes to their positions
        node_positions = {node: G.nodes[node]['position'] for node in G.nodes}
        elarge = [(u, v) for (u, v, d) in G.edges(data=True) if d["weight"] > 4]
        esmall = [(u, v) for (u, v, d) in G.edges(data=True) if d["weight"] <= 4]
        
        # Create the network plot
        plt.figure(figsize=(10, 10))
        #nx.draw(G, pos=node_positions, with_labels=True, node_size=300, node_color='skyblue', font_size=10, font_color='black', font_weight='bold')
        # nodes
        nx.draw_networkx_nodes(G, pos=node_positions, node_size=200, node_color='skyblue')
        # edges
        nx.draw_networkx_edges(G, pos=node_positions, edgelist=elarge, width=3)
        nx.draw_networkx_edges(G, pos=node_positions, edgelist=esmall, width=1, alpha=0.5)
        
        # node labels
        nx.draw_networkx_labels(G, pos=node_positions, font_size=8, font_family="sans-serif")
        # edge weight labels
        #edge_labels = nx.get_edge_attributes(G, "weight")
        #nx.draw_networkx_edge_labels(G, pos=node_positions, edge_labels=edge_labels)
        
        ax = plt.gca()
        ax.margins(0.08)
        plt.axis("off")
        plt.tight_layout()
        plt.show()
        
        # Show the plot
        plt.show()

    def map_matching(self, trajectory, verbose=False):
        '''
        A map-matching algorithm to map trajectories to paths.
        The algorithm maps a trajectory to a path on the graph, using the Leuven map matching algorithm (https://github.com/wannesm/LeuvenMapMatching)
        ====================================
        Params:
        trajectory: (MovingPandas Trajectory object) a trajectory
        ====================================
        Returns
        path_df: (GeoDataframe) contains the segments of the mapped path row by row 
        evaluation_results: (Dataframe) contains evaluation metrics about the mapped path with respect to the original trajectory
        '''
        G = self.G_pruned.copy()
        waypoints = self.waypoints.copy()
        connections = self.waypoint_connections_pruned.copy()
        points = trajectory.to_point_gdf()
        mmsi = points.mmsi.unique()[0]
        #print(mmsi)
        if verbose: 
            print('=======================')
            print(mmsi)
            print('=======================')
        
        ### GET potential START POINT ###
        orig_WP, idx_orig, dist_orig = geometry_utils.find_orig_WP(points, waypoints)
        
        ### GET potential END POINT ###
        dest_WP, idx_dest, dist_dest = geometry_utils.find_dest_WP(points, waypoints)
        if verbose: print('Potential start and end point:', orig_WP, dest_WP)

        ### GET ALL INTERSECTIONS between trajectory and waypoints and a channel around that trajectory that defines the subgraph
        passages, G_channel = geometry_utils.find_WP_intersections(points, trajectory, waypoints, G, 1000)
        if verbose: print('Intersections found:', passages)

        # convert graph to Leuven format
        converted_data = {}
        for node, data in G_channel.nodes(data=True):
            neighbors = list(G_channel.successors(node))
            position = data.get('position', None)
            converted_data[node] = (position, neighbors) 
        ll_map = InMemMap('MTM', graph=converted_data, use_latlon=True, crs_lonlat='EPSG:4326')
        
        # Distinguish three cases
        # 1. passages is empty and orig != dest
        if ((len(passages) == 0) & (orig_WP != dest_WP)):
            if dist_orig < 100:
                passages.append(orig_WP)
            if dist_dest < 100:
                passages.append(dest_WP)
        # 2. passages is empty and orig == dest --> nothing we can do here
        elif ((len(passages) == 0) & (orig_WP == dest_WP)):
            passages = []
        # 3. found passages
        else:
            # if the potential start waypoint is not in the list of intersections, but close to the origin of the trajectory, add it to the set of passages
            if ((orig_WP not in passages) & (dist_orig < 100) & (nx.has_path(G, orig_WP, passages[0]))):
                passages.insert(0, orig_WP)
            else:
                orig_WP = passages[0]
                orig_WP_point = waypoints[waypoints.clusterID==orig_WP]['geometry'].item()
                idx_orig = np.argmin(orig_WP_point.distance(points.geometry))
            
            # if the potential destination waypoint is not in the list of intersections, but close to the destination of the trajectory, add it to the set of passages
            if ((dest_WP not in passages) & (dist_dest < 100) & (nx.has_path(G, passages[-1], dest_WP))):
                passages.append(dest_WP)
            else:
                dest_WP = passages[-1]
                dest_WP_point = waypoints[waypoints.clusterID==dest_WP]['geometry'].item()
                idx_dest = np.argmin(dest_WP_point.distance(points.geometry))
        if verbose: print('Intersections with start and endpoint:', passages)
        
        if len(passages) >= 2:
            try:
                if verbose: print('Executing try statement')
                clipped_line, clipped_points = geometry_utils.clip_trajectory_between_WPs(trajectory, passages[0], passages[-1], waypoints)
                track = []
                for i in range(0, len(clipped_points)):
                    lon = clipped_points['lon'].iloc[i]
                    lat = clipped_points['lat'].iloc[i]
                    track.append(tuple([lon, lat]))
                # attempt with small radius
                #matcher = DistanceMatcher(ll_map, max_dist=1000, obs_noise=1000, min_prob_norm=0.01, max_lattice_width=5000)
                matcher = DistanceMatcher(ll_map, max_dist=10000, obs_noise=1000, min_prob_norm=0.1, max_lattice_width=10000)
                states, _ = matcher.match(track)
                path = matcher.path_pred_onlynodes
                message = 'success'
                if path == []:
                    message = 'failure'
                else:
                    if path[-1] != passages[-1]:
                        path = path[:-1]
                
                # Compute GeoDataFrame from path, containing the edge sequence as LineStrings
                path_df = pd.DataFrame(columns=['mmsi', 'orig', 'dest', 'geometry', 'message'])
                for j in range(0, len(path)-1):
                    #print(path[j], path[j+1])
                    edge = connections[(connections['from'] == path[j]) & (connections['to'] == path[j+1])].geometry.item()
                    temp = pd.DataFrame([[mmsi, path[j], path[j+1], edge, message]], columns=['mmsi', 'orig', 'dest', 'geometry', 'message'])
                    path_df = pd.concat([path_df, temp])
                path_df = gpd.GeoDataFrame(path_df, geometry='geometry', crs=self.crs)
        
                ###########
                # evaluate goodness of fit
                ###########
                if idx_orig >= idx_dest:  # In some cases this is needed, for example for roundtrips of ferries
                    idx_orig = 0
                    idx_dest = -1
                eval_points = points.iloc[idx_orig:idx_dest]  # the subset of points we are evaluating against
                multi_line = MultiLineString(list(path_df['geometry']))
                edge_sequence = ops.linemerge(multi_line)  # merge edge sequence to a single linestring
                # compute the fraction of trajectory that can be associated with an edge sequence
                t1 = points.index[idx_orig]
                t2 = points.index[idx_dest]
                try:
                    eval_traj = trajectory.get_linestring_between(t1, t2)
                    percentage_covered = eval_traj.length / trajectory.get_length()
                except:
                    eval_traj = trajectory
                    percentage_covered = 1
                num_points = len(eval_points)
                interpolated_points = [edge_sequence.interpolate(dist) for dist in range(0, int(edge_sequence.length)+1, int(edge_sequence.length/num_points)+1)]
                interpolated_points_coords = [Point(point.x, point.y) for point in interpolated_points]  # interpolated points on edge sequence
                interpolated_points = pd.DataFrame({'geometry': interpolated_points_coords})
                interpolated_points = gpd.GeoDataFrame(interpolated_points, geometry='geometry', crs=self.crs)    
                SSPD, d12, d21 = geometry_utils.sspd(eval_traj, eval_points['geometry'], edge_sequence, interpolated_points['geometry'])
                distances = d12.tolist() + d21.tolist()
                evaluation_results = pd.DataFrame({'mmsi':mmsi,
                                                   'SSPD':SSPD,
                                                   'distances':[distances],
                                                   'fraction_covered':percentage_covered,
                                                   'message':message,
                                                   'path':[path],
                                                   'path_linestring':edge_sequence}
                                                 )   
            # In some cases the above algorithm gets stuck in waypoints without any connections leading to the next waypoint
            # In this case we attempt to find the shortest path between origin and destination
            except:
                if verbose: print('Executing except statement...')
                # if a path exists, we compute it
                if nx.has_path(G, passages[0], passages[-1]):
                    path = nx.shortest_path(G, passages[0], passages[-1])
                    message = 'attempt'
                    # Compute GeoDataFrame from path, containing the edge sequence as LineStrings
                    path_df = pd.DataFrame(columns=['mmsi', 'orig', 'dest', 'geometry', 'message'])
                    for j in range(0, len(path)-1):
                        edge = connections[(connections['from'] == path[j]) & (connections['to'] == path[j+1])].geometry.item()
                        temp = pd.DataFrame([[mmsi, path[j], path[j+1], edge, message]], columns=['mmsi', 'orig', 'dest', 'geometry', 'message'])
                        path_df = pd.concat([path_df, temp])
                    path_df = gpd.GeoDataFrame(path_df, geometry='geometry', crs=self.crs)
            
                    ###########
                    # evaluate goodness of fit
                    ###########
                    if idx_orig >= idx_dest:  # In some cases this is needed, for example for roundtrips of ferries
                        idx_orig = 0
                        idx_dest = -1
                    eval_points = points.iloc[idx_orig:idx_dest]  # the subset of points we are evaluating against
                    multi_line = MultiLineString(list(path_df.geometry))
                    edge_sequence = ops.linemerge(multi_line)  # merge edge sequence to a single linestring
                    t1 = points.index[idx_orig]
                    t2 = points.index[idx_dest]
                    try:
                        eval_traj = trajectory.get_linestring_between(t1, t2)
                        percentage_covered = eval_traj.length / trajectory.get_length()
                    except:
                        eval_traj = trajectory
                        percentage_covered = 1
                    num_points = len(eval_points)
                    interpolated_points = [edge_sequence.interpolate(dist) for dist in range(0, int(edge_sequence.length)+1, int(edge_sequence.length/num_points)+1)]
                    interpolated_points_coords = [Point(point.x, point.y) for point in interpolated_points]  # interpolated points on edge sequence
                    interpolated_points = pd.DataFrame({'geometry': interpolated_points_coords})
                    interpolated_points = gpd.GeoDataFrame(interpolated_points, geometry='geometry', crs=self.crs)    
                    SSPD, d12, d21 = geometry_utils.sspd(eval_traj, eval_points['geometry'], edge_sequence, interpolated_points['geometry'])
                    distances = d12.tolist() + d21.tolist()
                    evaluation_results = pd.DataFrame({'mmsi':mmsi,
                                                       'SSPD':SSPD,
                                                       'distances':[distances],
                                                       'fraction_covered':percentage_covered,
                                                       'message':message,
                                                       'path':[path],
                                                       'path_linestring':edge_sequence}
                                                     )
                # If there is no path between origin and destination, we cannot map the trajectory to an edge sequence
                else:
                    message = 'no_path'
                    path_df = pd.DataFrame({'mmsi':mmsi, 'orig':orig_WP, 'dest':dest_WP, 'geometry':[], 'message':message})
                    evaluation_results = pd.DataFrame({'mmsi':mmsi,
                                                       'SSPD':np.nan,
                                                       'distances':[np.nan],
                                                       'fraction_covered':0,
                                                       'message':message,
                                                       'path':[np.nan],
                                                       'path_linestring':np.nan}
                                                     )
        # When there are no intersections with any waypoints, we cannot map the trajectory to an edge sequence
        else:
            #print('Not enough intersections found. Cannot map trajectory to graph...')
            message = 'no_intersects'
            #print(mmsi, ': failure')
            path_df = pd.DataFrame({'mmsi':mmsi, 'orig':orig_WP, 'dest':dest_WP, 'geometry':[], 'message':message})
            evaluation_results = pd.DataFrame({'mmsi':mmsi,
                                               'SSPD':np.nan,
                                               'distances':[np.nan],
                                               'fraction_covered':0,
                                               'message':message,
                                               'path':[np.nan],
                                               'path_linestring':np.nan}
                                             )
        return path_df, evaluation_results