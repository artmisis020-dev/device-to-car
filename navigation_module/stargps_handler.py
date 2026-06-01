from collections import deque
import dataclasses
from datetime import datetime
import math
from typing import Deque, Iterable, Optional, List


@dataclasses.dataclass
class GPSRecord:
    timestamp: datetime
    lat: float
    lon: float
    alt: float = 0.0
    speed: float = 0.0      # m/s, filled by GPSWindow
    az: float = 0.0         # azimuth in degrees, 0..360, filled by GPSWindow


class StarGPSHandler:
    """
    Fixed-size window of last N GPS records.
    On append, it:
      - computes speed (m/s) and azimuth (deg) from previous record
      - maintains a running sum of speeds for O(1) avg_speed
    """
    

    def __init__(self, maxlen: int = 100):
        self._maxlen = maxlen
        self._data: Deque[GPSRecord] = deque()

         # speed stats
        self._speed_sum: float = 0.0
        self._speed_sumsq: float = 0.0

        # azimuth stats (angles in radians internally)
        self._az_sin_sum: float = 0.0
        self._az_cos_sum: float = 0.0        

        # the difference between last and previous added records in meters
        self._last_dist_diff = 0.0

    @staticmethod
    def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        """Distance between two points on Earth, in meters."""
        R = 6_371_000.0  # Earth radius in meters

        phi1 = math.radians(lat1)
        phi2 = math.radians(lat2)
        dphi = math.radians(lat2 - lat1)
        dlambda = math.radians(lon2 - lon1)

        a = math.sin(dphi / 2.0) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2.0) ** 2
        c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))

        return R * c

    @staticmethod
    def _bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        """
        Initial bearing (azimuth) from point 1 to point 2 in degrees [0, 360).
        0° = North, 90° = East, 180° = South, 270° = West.
        """
        phi1 = math.radians(lat1)
        phi2 = math.radians(lat2)
        dlon = math.radians(lon2 - lon1)

        x = math.sin(dlon) * math.cos(phi2)
        y = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dlon)

        bearing_rad = math.atan2(x, y)          # [-pi, pi]
        bearing_deg = math.degrees(bearing_rad) # [-180, 180]
        bearing_deg = (bearing_deg + 360.0) % 360.0

        return bearing_deg
    
    @staticmethod
    def _bearing_difference_deg(a: float, b: float) -> float:
        """
        Return the smallest angle difference between two bearings (in degrees).
        Result is in [0, 180].
        """
        # Normalize inputs to [0, 360)
        a = a % 360
        b = b % 360

        # Compute signed difference in range (-180, 180]
        diff = (b - a + 180) % 360 - 180

        # Return absolute value for the smallest unsigned difference
        return abs(diff)    

    def append(self, record: GPSRecord) -> GPSRecord:
        """
        Append a GPSRecord.
        - Computes speed/az from previous record if exists.
        - Keeps only last `maxlen` records.
        - Maintains running sum of speeds.
        """
        if self._data:
            prev = self._data[-1]

            # time difference in seconds
            dt = (record.timestamp - prev.timestamp).total_seconds()

            # distance in meters between prev and current
            self._last_dist_diff = self._haversine_m(prev.lat, prev.lon, record.lat, record.lon)

            # speed in m/s (protect from dt<=0)
            record.speed = self._last_dist_diff / dt if dt > 0 else 0.0

            # azimuth in degrees
            record.az = self._bearing_deg(prev.lat, prev.lon, record.lat, record.lon)
        else:
            # first point in window
            record.speed = 0.0
            record.az = 0.0

          # if window is full, remove oldest and update sums
        if len(self._data) == self._maxlen:
            oldest = self._data.popleft()
             # speed stats
            self._speed_sum   -= oldest.speed
            self._speed_sumsq -= oldest.speed * oldest.speed

            # azimuth stats
            old_az_rad = math.radians(oldest.az)
            self._az_sin_sum -= math.sin(old_az_rad)
            self._az_cos_sum -= math.cos(old_az_rad)

        # add new record and update sums
        self._data.append(record)

         # speed stats
        self._speed_sum   += record.speed
        self._speed_sumsq += record.speed * record.speed

        # azimuth stats
        az_rad = math.radians(record.az)
        self._az_sin_sum += math.sin(az_rad)
        self._az_cos_sum += math.cos(az_rad)           

        return record

    @property
    def avg_speed(self) -> float:
        """
        Average speed (m/s) over current window, O(1).
        Returns 0.0 if window is empty.
        """
        n = len(self._data)
        if n == 0:
            return 0.0
        return self._speed_sum / n
    
    @property
    def std_speed(self) -> float:
        """
        Sample standard deviation of speed (m/s) over current window.
        Returns NaN if fewer than 2 samples.
        """
        n = len(self._data)
        if n < 2:
            return float('nan')

        mean = self._speed_sum / n
        # population variance
        var = (self._speed_sumsq / n) - mean * mean
        var = max(var, 0.0)  # numerical safety
        # convert to sample variance (ddof=1)
        var *= n / (n - 1)
        return math.sqrt(var)
    
    @property
    def span_speed(self) -> float:
        """
        Average speed (m/s) between first and last record:
        straight-line distance(first, last) / total time.
        Returns NaN if fewer than 2 records or non-positive dt.
        """
        n = len(self._data)
        if n < 2:
            return float('nan')

        first = self._data[0]
        last  = self._data[-1]

        dt = (last.timestamp - first.timestamp).total_seconds()
        if dt <= 0:
            return float('nan')

        dist_m = self._haversine_m(first.lat, first.lon, last.lat, last.lon)
        return dist_m / dt
    
    # ----------------- azimuth stats (circular) -----------------

    @property
    def avg_azimuth(self) -> float:
        """
        Circular mean azimuth in DEGREES [0, 360).
        Returns NaN if window is empty.
        """
        n = len(self._data)
        if n == 0:
            return float('nan')

        # mean direction vector
        mean_sin = self._az_sin_sum / n
        mean_cos = self._az_cos_sum / n

        mean_angle_rad = math.atan2(mean_sin, mean_cos)  # [-pi, pi]
        mean_angle_deg = math.degrees(mean_angle_rad)
        mean_angle_deg = (mean_angle_deg + 360.0) % 360.0

        return mean_angle_deg
    
    @property
    def std_azimuth(self) -> float:
        """
        Circular standard deviation of azimuth in DEGREES.
        Uses definition: s = sqrt(-2 ln R), R = mean resultant length.
        Returns NaN if fewer than 2 samples.
        """
        n = len(self._data)
        if n < 2:
            return float('nan')

        # resultant length R
        mean_sin = self._az_sin_sum / n
        mean_cos = self._az_cos_sum / n
        R = math.hypot(mean_sin, mean_cos)

        # numerical safety
        R = min(max(R, 1e-12), 1.0)

        # circular std in radians, then convert to degrees
        circ_std_rad = math.sqrt(-2.0 * math.log(R))
        circ_std_deg = math.degrees(circ_std_rad)

        return circ_std_deg
    
    @property
    def span_azimuth(self) -> float:
        """
        Azimuth (deg, 0..360) from FIRST record to LAST record.
        Returns NaN if fewer than 2 records.
        """
        #record.az = self._bearing_deg(prev.lat, prev.lon, record.lat, record.lon)
        n = len(self._data)
        if n < 2:
            return float('nan')

        first = self._data[0]
        last  = self._data[-1]

        return self._bearing_deg(first.lat, first.lon, last.lat, last.lon)    
    
    # the difference between last and previous added records in meters
    @property 
    def last_dist_diff(self) -> float:
        return self._last_dist_diff

    
    # Convenience accessors
    def __len__(self) -> int:
        return len(self._data)

    def __iter__(self) -> Iterable[GPSRecord]:
        return iter(self._data)

    def __getitem__(self, idx: int) -> GPSRecord:
        return self._data[idx]

    def latest(self) -> Optional[GPSRecord]:
        """Return last record or None if empty."""
        return dataclasses.replace(self._data[-1]) if self._data else None
    def first(self) -> Optional[GPSRecord]:
        """Return first record or None if empty."""
        return self._data[0] if self._data else None
    
    #Project move from (lat_deg, lon_deg) along 'bearing_deg' for 'distance_m' meters
    def project_point(self, lat_deg: float, lon_deg: float,
                  bearing_deg: float, distance_m: float) -> (float, float):
        
        EARTH_RADIUS_M = 6_371_000.0  # mean Earth radius in meters
    
        # Convert to radians
        lat1 = math.radians(lat_deg)
        lon1 = math.radians(lon_deg)
        bearing = math.radians(bearing_deg)

        # Angular distance
        ang_dist = distance_m / EARTH_RADIUS_M

        # Forward geodesic formula
        sin_lat1 = math.sin(lat1)
        cos_lat1 = math.cos(lat1)
        sin_ang = math.sin(ang_dist)
        cos_ang = math.cos(ang_dist)
        sin_bear = math.sin(bearing)
        cos_bear = math.cos(bearing)

        lat2 = math.asin(sin_lat1 * cos_ang + cos_lat1 * sin_ang * cos_bear)
        lon2 = lon1 + math.atan2(
            sin_bear * sin_ang * cos_lat1,
            cos_ang - sin_lat1 * math.sin(lat2)
        )

        # Normalize lon to [-180, 180)
        lon2 = (lon2 + math.pi) % (2 * math.pi) - math.pi

        return math.degrees(lat2), math.degrees(lon2)

    #Generate forecasted positions for each second ahead, assuming constant speed and bearing.
    #Returns a list of ForecastPoint        
    def forecast_track(self,
        start_lat: float,
        start_lon: float,
        az_deg: float,
        speed: float,
        seconds: int, 
        frequency: int
    ) -> List[GPSRecord]:
        
        result: List[GPSRecord] = []
        
        for t in range(1, seconds * frequency + 1):
            distance = speed * t/ frequency # meters traveled from start after t seconds
            lat_t, lon_t = self.project_point(start_lat, start_lon, az_deg, distance)
            result.append(GPSRecord(timestamp=t, lat=lat_t, lon=lon_t, speed=speed, az=az_deg))

        return result
