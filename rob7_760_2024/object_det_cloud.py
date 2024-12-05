import rclpy
from rclpy.node import Node
from sensor_msgs.msg import PointCloud2, PointField
from geometry_msgs.msg import PointStamped
import tf2_ros
from tf2_geometry_msgs import do_transform_point
import struct
import math
import sensor_msgs_py.point_cloud2 as pc2


class MapBuilder(Node):
    def __init__(self):
        super().__init__('map_builder')

        # Subscribe to the PointCloud2 topic from the SegmentationNode
        self.pointcloud_sub = self.create_subscription(
            PointCloud2, '/object_detected/pointcloud', self.pointcloud_callback, 10)

        # Publisher for the transformed points as PointCloud2
        self.point_cloud_pub = self.create_publisher(PointCloud2, '/transformed_points', 10)

        # Create a TF buffer and listener to get the transforms
        self.tf_buffer = tf2_ros.Buffer(rclpy.duration.Duration(seconds=100))


        # List to store transformed points with labels
        self.transformed_points = []

    def pointcloud_callback(self, msg):
        """
        Callback for the /object_detected/pointcloud topic.
        Processes points and transforms them to the map frame.
        """
        # Get the timestamp from the PointCloud2 message
        timestamp = msg.header.stamp

       # Wait for the transform to be available using the PointCloud2's timestamp
        self.tf_listener = tf2_ros.TransformListener(self.tf_buffer, self)
        try:
            self.get_logger().info(f"tf_buffer: '{self.tf_buffer}'")
            transform = self.tf_buffer.lookup_transform(
                'map',  # Target frame
                msg.header.frame_id,  # Source frame
                rclpy.time.Time.from_msg(timestamp),  # Timestamp from the message
                timeout=rclpy.duration.Duration(seconds=0.01)  # Adjust timeout as needed
            )

            # Calculate the time difference between the point cloud timestamp and the transform
            time_diff = ((transform.header.stamp - timestamp).nanoseconds)  # In seconds
            time_diff = time_diff/ 1e9 
            self.get_logger().info(f"Using transform (time diff: {time_diff:.3f} seconds)")

            if time_diff > 0.1:  # If the time difference is greater than 0.1 seconds, skip processing
                self.get_logger().warn(f"Transform is too old ({time_diff:.3f} seconds), skipping point cloud processing.")
                return

            self.get_logger().info(f"Using transform (time diff: {time_diff:.3f} seconds)")

        except (tf2_ros.LookupException, tf2_ros.ConnectivityException, tf2_ros.ExtrapolationException) as e:
            self.get_logger().warn(f"Transform lookup failed: {e}. Skipping point cloud processing.")
            return
        
        # Extract points from the PointCloud2 message
        points = self.extract_points_from_pointcloud2(msg)

        # Reduce the number of points by filtering based on proximity
        points = self.reduce_points(points)

        # Transform each point and store it
        for point in points:
            transformed_point = self.transform_point(point, transform)
            if transformed_point:  # Check if transformation succeeded
                # Check if this point is too close to any point in the transformed points list
                if self.is_point_too_close(transformed_point):
                    continue  # Skip the point if it's too close to an existing point
                
                # Combine point and label in a single dictionary
                transformed_point['label'] = point['label']
                self.transformed_points.append(transformed_point)

        # Publish the transformed points as PointCloud2
        self.publish_point_cloud()

    def extract_points_from_pointcloud2(self, msg):
        """
        Extracts points with labels from a PointCloud2 message.
        """
        points = []
        for point in pc2.read_points(msg, field_names=('x', 'y', 'z', 'label'), skip_nans=True):
            x, y, z, label = point
            points.append({'x': x, 'y': y, 'z': z, 'label': int(label)})
        return points

    def is_valid_point(self, point):
        """
        Checks if a point has valid numerical values for x, y, and z.
        """
        return not any(math.isnan(point[dim]) or math.isinf(point[dim]) for dim in ['x', 'y', 'z'])

    def transform_point(self, point, transform):
        """
        Transforms a point from the source frame to the target frame.
        """
        if transform is None:
            self.get_logger().error("Transform is None. Skipping point transformation.")
            return None

        try:
            # Check if transform contains NaN or Inf values
            translation = transform.transform.translation
            rotation = transform.transform.rotation
            if any(math.isnan(val) or math.isinf(val) for val in [translation.x, translation.y, translation.z]):
                self.get_logger().error(f"Transform contains invalid translation values: {translation}")
                return None
            if any(math.isnan(val) or math.isinf(val) for val in [rotation.x, rotation.y, rotation.z, rotation.w]):
                self.get_logger().error(f"Transform contains invalid rotation values: {rotation}")
                return None

            point_stamped = PointStamped()
            point_stamped.header.frame_id = transform.header.frame_id
            point_stamped.point.x = point['x']
            point_stamped.point.y = point['y']
            point_stamped.point.z = point['z']

            transformed_point_stamped = do_transform_point(point_stamped, transform)
            return {
                'x': transformed_point_stamped.point.x,
                'y': transformed_point_stamped.point.y,
                'z': transformed_point_stamped.point.z,
            }
        except Exception as e:
            self.get_logger().error(f"Failed to transform point: {e}")
            return None

    def publish_point_cloud(self):
        """
        Publish transformed points as a PointCloud2 message for visualization in RViz2.
        Publishes x, y, z coordinates and label_id.
        """
        if not self.transformed_points:
            return

        # Create a PointCloud2 message
        cloud_msg = PointCloud2()
        cloud_msg.header.stamp = self.get_clock().now().to_msg()
        cloud_msg.header.frame_id = 'map'  # Use the map frame for visualization

        # Define the PointField structure for x, y, z, and label
        cloud_msg.height = 1
        cloud_msg.width = len(self.transformed_points)
        cloud_msg.fields = [
            PointField(name='x', offset=0, datatype=PointField.FLOAT32, count=1),
            PointField(name='y', offset=4, datatype=PointField.FLOAT32, count=1),
            PointField(name='z', offset=8, datatype=PointField.FLOAT32, count=1),
            PointField(name='label', offset=12, datatype=PointField.UINT32, count=1),  # Add label field
        ]
        cloud_msg.is_bigendian = False
        cloud_msg.point_step = 16  # Each point consists of 3 floats (x, y, z) and 1 uint32 (label)
        cloud_msg.row_step = cloud_msg.point_step * cloud_msg.width
        cloud_msg.is_dense = True

        # Serialize the point data with labels
        cloud_data = []
        for point in self.transformed_points:
            cloud_data.append(struct.pack('fffI', point['x'], point['y'], point['z'], point['label']))
        cloud_msg.data = b''.join(cloud_data)

        # Publish the PointCloud2 message
        self.point_cloud_pub.publish(cloud_msg)
        self.get_logger().info(f"Published PointCloud2 with {len(self.transformed_points)} points")

    def reduce_points(self, points):
        """
        Reduces the number of points by keeping only those that are not
        within a specified distance of another point.
        """
        distance_threshold = 0.002  # Minimum distance between points
        filtered_points = []

        for point in points:
            if not self.is_valid_point(point):
                self.get_logger().warn(f"Invalid point detected and skipped: {point}")
                continue

            too_close = False
            for f_point in filtered_points:
                if not self.is_valid_point(f_point):  # Skip invalid points in filtered points list
                    continue
                dist = math.sqrt(
                    (point['x'] - f_point['x']) ** 2 +
                    (point['y'] - f_point['y']) ** 2 +
                    (point['z'] - f_point['z']) ** 2  # Ensure valid z values
                )
                if dist < distance_threshold:
                    too_close = True
                    break
            if not too_close:
                filtered_points.append(point)

        return filtered_points

    def is_point_too_close(self, point):
        """
        Check if the point is too close to any other point in the output point cloud.
        """
        if not self.is_valid_point(point):
            self.get_logger().warn(f"Point contains invalid values and will be skipped: {point}")
            return True

        distance_threshold = 0.01  # Minimum distance between points

        for f_point in self.transformed_points:
            if not self.is_valid_point(f_point):  # Ensure existing transformed point is valid
                self.get_logger().warn(f"Existing transformed point contains invalid values: {f_point}")
                continue

            dist = math.sqrt(
                (point['x'] - f_point['x']) ** 2 +
                (point['y'] - f_point['y']) ** 2 +
                (point['z'] - f_point['z']) ** 2  # Ensure valid z values
            )
            if dist < distance_threshold:
                return True  # The point is too close to an existing point

        return False


def main(args=None):
    rclpy.init(args=args)
    node = MapBuilder()
    rclpy.spin(node)
    rclpy.shutdown()


if __name__ == '__main__':
    main()