#!/usr/bin/env python

import rospy
from extended_object_detection.msg import SimpleObjectArray, ComplexObjectArray
from object_spatial_tools_ros.utils import obj_transform_to_pose, get_common_transform, get_cov_ellipse_params, quaternion_msg_from_yaw, multi_mahalanobis
import tf2_geometry_msgs
import tf2_ros
import tf
import numpy as np
from visualization_msgs.msg import Marker, MarkerArray
from threading import Lock
from geometry_msgs.msg import PoseStamped, TransformStamped, Point, Quaternion, Pose
from object_spatial_tools_ros.msg import TrackedObject, TrackedObjectArray

class SingleKFUndirectedObjectTracker(object):
    
    '''        
    x_start - start pose state [x, y]
    t_start - time stamp for init
    Q_list  - diagonale of Q matrix [Qx, Qy, Qvx, Qvy]
    R_diag  - diagonale of R matrix [Rx, Ry]    
    k_decay - coefficient of speed reduction, default = 1 
    Z       - state of a system
    '''
    def __init__(self, x_start, t_start, Q_diag, R_diag, k_decay = 1, orient = [0, 0, 0, 1]):
        
        self.Z = np.array([x_start[0], x_start[1], 0.0, 0.0]) # instead of self.x
        self.last_t = t_start
        self.last_upd_t = t_start
        self.Q = np.diag(Q_diag)
        self.R = np.diag(R_diag)
        self.k_decay = k_decay
        self.rotation = Quaternion(orient[0], orient[1], orient[2], orient[3])
        
        
        self.H = np.array([[1, 0, 0, 0],
                           [0, 1, 0, 0]])
        
        self.P = np.eye(4)
        
        self.I = np.eye(4)                            
        
        self.track = []
        
    '''
    t - time stamp for predict, seconds
    '''
    def predict(self, t):        
        dt = t - max(self.last_t, self.last_upd_t)
        self.last_t = t
        
        F = np.array([[1, 0, dt ,0],
                           [0, 1, 0 ,dt],
                          ## [0, 0, 0, 0],
                          ## [0, 0, 0, 0]])
                           [0, 0, self.k_decay, 0],
                           [0, 0, 0, self.k_decay]])
        
        self.Z = np.matmul(F, self.Z) #self.Z
        
        self.P = np.matmul( np.matmul(F, self.P), F.T) + self.Q
        
        self.track.append(self.Z.copy())
        
            
    '''
    y_measured - measured x, y values
    t          - time stamp for update, seconds
    '''
    def update(self, y_measured, t, orient):
        self.last_upd_t = t
                
        Y = y_measured - np.matmul(self.H, self.Z)
        
        S = np.matmul( np.matmul(self.H, self.P), self.H.T) + self.R  # intemediary calculations
        
        G = np.matmul( np.matmul(self.P, self.H.T), np.linalg.inv(S)) # instead of K
        
        self.Z = self.Z + np.matmul(G, Y)
        
        self.P = np.matmul((self.I - np.matmul(G, self.H)), self.P)
        
        self.rotation = Quaternion(orient[0], orient[1], orient[2], orient[3])
        
        self.track.append(self.Z.copy())

class RobotKFUndirectedObjectTracker(object):
    
    def __init__(self):
        
        rospy.init_node('robot_kf_undirected_object_tracker')
        
        self.target_frame = rospy.get_param('~target_frame', 'odom')
        self.tf_pub_prefix = rospy.get_param('~tf_pub_prefix', '')
        if self.tf_pub_prefix != '':
            self.tf_pub_prefix = self.tf_pub_prefix + '/'
        
        tracked_objects_type_names = rospy.get_param('~tracked_objects_type_names', [])
        #надо заставить отслеживать объект с определённым id
        
        self.objects_to_KFs = {}
        self.KFs_prev_elements = {}
        for type_obj in tracked_objects_type_names:
            self.objects_to_KFs[type_obj] = []            
            self.KFs_prev_elements[type_obj] = 0
        
        self.tf_buffer = tf2_ros.Buffer(rospy.Duration(100.0))  # tf buffer length
        self.tf_listener = tf2_ros.TransformListener(self.tf_buffer)
        self.tf_broadcaster = tf2_ros.TransformBroadcaster()
        
        self.Qdiag = rospy.get_param('~Qdiag', [0.1, 0.1, 0.1, 0.1])
        self.Rdiag = rospy.get_param('~Rdiag', [0.7, 0.7])
        self.k_decay = rospy.get_param('~k_decay', 1)
        self.lifetime = rospy.get_param('~lifetime', 0)
        self.mahalanobis_max = rospy.get_param('~mahalanobis_max', 1)
                
        self.min_score = rospy.get_param('~min_score', 0)
        self.min_score_soft = rospy.get_param('~min_score_soft', self.min_score)
        
        
        self.mutex = Lock()
        
        update_rate_hz = rospy.get_param('~update_rate_hz', 5)
        
        self.vis_pub = rospy.Publisher('~vis', MarkerArray, queue_size = 1)

        self.out_pub = rospy.Publisher('~tracked_objects', TrackedObjectArray, queue_size = 1)
        
        rospy.Subscriber('simple_objects', SimpleObjectArray, self.sobject_cb)
        rospy.Subscriber('complex_objects', ComplexObjectArray, self.cobject_cb)
        
        self.temp_pub_1 = rospy.Publisher('temp_pub_1', PoseStamped, queue_size = 1)
        self.temp_pub_2 = rospy.Publisher('temp_pub_2', Pose, queue_size = 1)
        
        rospy.Timer(rospy.Duration(1/update_rate_hz), self.process)
        
    def process(self, event):
        self.mutex.acquire()
        now = rospy.Time.now().to_sec()
        for name, kfs in self.objects_to_KFs.items():
            self.KFs_prev_elements[name] = len(kfs)
            remove_index = []
            for i, kf in enumerate(kfs):
                if self.lifetime == 0 or (now - kf.last_upd_t) < self.lifetime:
                    kf.predict(now)
                else:
                    remove_index.append(i)
        
                #rospy.logwarn(f"{name} {i} {kf.Z} {kf.P}")
            for index in sorted(remove_index, reverse=True):
                del kfs[index]
                
        self.to_marker_array()
        self.to_tf()
        self.to_tracked_object_array()
        self.mutex.release()
        
    def to_tf(self):
        now = rospy.Time.now()
        for name, kfs in self.objects_to_KFs.items():
            for i, kf in enumerate(kfs):
                
                t = TransformStamped()
                
                t.header.stamp = now
                t.header.frame_id = self.target_frame
                t.child_frame_id = self.tf_pub_prefix+name+f'_{i}'
                
                t.transform.translation.x = kf.Z[0]
                t.transform.translation.y = kf.Z[1]
                
                t.transform.rotation = kf.rotation
                
                self.tf_broadcaster.sendTransform(t)                
        
    
    def to_tracked_object_array(self):        
        msg_array = TrackedObjectArray()
        msg_array.header.stamp = rospy.Time.now()
        msg_array.header.frame_id = self.target_frame
        for name, kfs in self.objects_to_KFs.items():
            for i, kf in enumerate(kfs):
                msg = TrackedObject()
                msg.child_frame_id = self.tf_pub_prefix+name+f'_{i}'
                
                msg.pose.pose.position.x = kf.Z[0]
                msg.pose.pose.position.y = kf.Z[1]
                
                msg.pose.pose.orientation.w = 1
                
                msg.pose.covariance[0] = kf.P[0,0]#xx
                msg.pose.covariance[1] = kf.P[0,1]#xy
                msg.pose.covariance[6] = kf.P[1,0]#yx
                msg.pose.covariance[7] = kf.P[1,1]#yy
                
                msg.twist.twist.linear.x = kf.Z[2]
                msg.twist.twist.linear.y = kf.Z[3]
                
                msg.twist.covariance[0] = kf.P[2,2]#xx
                msg.twist.covariance[1] = kf.P[2,3]#xy
                msg.twist.covariance[2] = kf.P[3,2]#yx
                msg.twist.covariance[3] = kf.P[3,3]#yy
                
                msg_array.objects.append(msg)
                
        self.out_pub.publish(msg_array)
                
                
    def to_marker_array(self):
        now = rospy.Time.now()
        marker_array = MarkerArray()
        for name, kfs in self.objects_to_KFs.items():
            i = 0
            for i, kf in enumerate(kfs):
                # POSE AND SPEED
                marker = Marker()
                
                marker.header.stamp = now
                marker.header.frame_id = self.target_frame
                
                marker.ns = name+"_pose"
                marker.id = i
                
                marker.type = Marker.ARROW
                marker.action = Marker.ADD
                
                marker.pose.position.x = kf.Z[0]
                marker.pose.position.y = kf.Z[1]                                                
                
                marker.pose.orientation = quaternion_msg_from_yaw(np.arctan2(kf.Z[3], kf.Z[2]))
                                
                marker.color.r = 1
                marker.color.a = 1
                
                marker.scale.x = np.hypot(kf.Z[3], kf.Z[2])
                marker.scale.y = 0.01
                marker.scale.z = 0.01
                
                marker_array.markers.append(marker)
                
                # COV ELLIPSE
                marker = Marker()
                
                marker.header.stamp = now
                marker.header.frame_id = self.target_frame
                
                marker.ns = name+"_el"
                marker.id = i
                
                marker.type = Marker.CYLINDER
                marker.action = Marker.ADD
                
                marker.pose.position.x = kf.Z[0]
                marker.pose.position.y = kf.Z[1]                
                                
                marker.color.r = 1
                marker.color.b = 1
                marker.color.a = 0.5
                
                r1, r2, th = get_cov_ellipse_params(kf.P[:2,:2])
                
                marker.scale.x = r1
                marker.scale.y = r2
                marker.scale.z = 0.01
                marker.pose.orientation = quaternion_msg_from_yaw(th)
                
                marker_array.markers.append(marker)
                
                # TRACK
                marker = Marker()
                
                marker.header.stamp = now
                marker.header.frame_id = self.target_frame
                
                marker.ns = name+"_track"
                marker.id = i
                
                marker.type = Marker.LINE_STRIP
                marker.action = Marker.ADD
                
                marker.scale.x = 0.03
                marker.color.g = 1
                marker.color.a = 1
                marker.pose.orientation.w = 1
                
                for track_item in kf.track:
                    p = Point()
                    p.x = track_item[0]
                    p.y = track_item[1]
                    marker.points.append(p)
                    
                marker_array.markers.append(marker)
                
                
            for j in range(i+1, self.KFs_prev_elements[name]):
                for t in ["_el","_pose", "_track"]:
                    marker = Marker()                
                    marker.header.stamp = now
                    marker.ns = name+t
                    marker.id = j
                    marker.action = Marker.DELETE
                    marker_array.markers.append(marker)
                
        self.vis_pub.publish(marker_array)
            
    
    def sobject_cb(self, msg):
        self.mutex.acquire()
        now = rospy.Time.now().to_sec()
        transform = get_common_transform(self.tf_buffer, msg.header, self.target_frame)
        if transform is None:
            self.mutex.release()
            return
                
        self.proceed_objects(msg.header, msg.objects, transform, now)
        self.mutex.release()
        
    def cobject_cb(self, msg):
        self.mutex.acquire()
        now = rospy.Time.now().to_sec()
        transform = get_common_transform(self.tf_buffer, msg.header, self.target_frame)
        if transform is None:
            self.mutex.release()
            return
        
        self.proceed_objects(msg.header, msg.complex_objects, transform, now)
        self.mutex.release()
        
        
    def proceed_objects(self, header, objects, transform, now):
        detected_objects = {}
        for obj in objects:            
            if obj.type_name in self.objects_to_KFs:                                
                
                soft_tracking = False
                
                if obj.transform.translation.z == 1:
                    continue
                
                if obj.score < self.min_score:
                    if obj.score < self.min_score_soft:
                        continue    
                    soft_tracking = True
                
                ps = obj_transform_to_pose(obj.transform, header)
                #self.temp_pub_1.publish(ps)
                #положение в поле зрения камеры
                
                ps_transformed = tf2_geometry_msgs.do_transform_pose(ps, transform).pose
                #self.temp_pub_2.publish(ps_transformed)
                #положение в системе координат, связанной с target_frame (odom по умолчанию)
                #здесь ориентация, полученная системой распознавания, ещё не утрачена
                
                ps_np = np.array([ps_transformed.position.x, ps_transformed.position.y, float(soft_tracking), ps_transformed.orientation.x, ps_transformed.orientation.y, ps_transformed.orientation.z, ps_transformed.orientation.w])
                #добавлены последние 4 элемента, отвечающие за ориентацию
                
                if obj.type_name in detected_objects:
                    detected_objects[obj.type_name].append(ps_np)
                else:
                    detected_objects[obj.type_name] = [ps_np]                                
                
        for obj_name, poses in detected_objects.items():
            
            if len(self.objects_to_KFs[obj_name]) == 0:
                for pose in poses:                
                    if pose[2] == 1:
                        continue # skip soft_tracking
                    self.objects_to_KFs[obj_name].append(SingleKFUndirectedObjectTracker(pose[:2], now, self.Qdiag, self.Rdiag, self.k_decay, pose[3:]))
                    #в конструктор добавлена ориентация
            else:
                
                m_poses_np = np.array(poses)[:,:2] # [[x, y]]
                
                kf_poses_np = np.array([[kf.Z[0], kf.Z[1]] for kf in self.objects_to_KFs[obj_name]])
                
                S_np = np.array([np.linalg.inv(kf.P[:2,:2]) for kf in self.objects_to_KFs[obj_name]]) # already inv!
                
                D = multi_mahalanobis(m_poses_np, kf_poses_np, S_np)
                
                #print('D', D, D.shape)
                
                extra_poses = list(range(len(poses)))
                while not rospy.is_shutdown():
                    
                    closest = np.unravel_index(np.argmin(D, axis=None), D.shape)
                    #print(closest, D[closest])
                                    
                    if D[closest] > self.mahalanobis_max:
                        break
                                        
                    self.objects_to_KFs[obj_name][closest[1]].update(poses[closest[0]][:2], now, poses[closest[0]][3:])
                    
                    D[closest[0],:] = np.inf
                    D[:,closest[1]] = np.inf
                    extra_poses.remove(closest[0])
                                                        
                #for i in range(D.shape[0]):
                for i in extra_poses:      
                    if poses[i][2] == 1: # soft_tracking
                        continue
                    self.objects_to_KFs[obj_name].append(SingleKFUndirectedObjectTracker(poses[i][:2], now, self.Qdiag, self.Rdiag, self.k_decay, poses[3:]))        
        
        
                        
        
    def run(self):
        rospy.spin()
