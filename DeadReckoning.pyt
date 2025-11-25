# -*- coding: utf-8 -*-
"""
PredictVesselPositions.py

ArcGIS Pro script tool that:
- Reads lat, lon, heading (deg), speed (knots)
- Uses a time interval (hours) to project positions geodesically
- Flags predicted positions that fall on landmasses (mask polygons)
- Optionally generates forked/banded predictions around landmasses and
  estimates proximity to land

Assumes input lat/lon are WGS 84 (EPSG:4326) or at least geographic degrees.
"""

import arcpy
import datetime


def main():
    arcpy.env.overwriteOutput = True

    # --- Script tool parameters ---
    in_table = arcpy.GetParameterAsText(0)      # Input vessels table/FC
    lat_field = arcpy.GetParameterAsText(1)     # Latitude field
    lon_field = arcpy.GetParameterAsText(2)     # Longitude field
    hdg_field = arcpy.GetParameterAsText(3)     # Heading field (deg, nautical)
    spd_field = arcpy.GetParameterAsText(4)     # Speed field (knots)
    time_hours = float(arcpy.GetParameterAsText(5))  # Time interval in hours
    land_mask = arcpy.GetParameterAsText(6)     # Land polygons
    out_fc = arcpy.GetParameterAsText(7)        # Output predicted points
    time_baseline_txt = arcpy.GetParameterAsText(8)  # Optional baseline datetime
    land_band_m = arcpy.GetParameterAsText(9)   # Buffer distance (m) for land banding
    band_count_txt = arcpy.GetParameterAsText(10)    # 2 or 3 band outcomes

    # --- Constants ---
    KNOT_TO_M = 1852.0  # meters per knot-hour
    DEFAULT_BAND_DISTANCE_M = 1000.0
    DEFAULT_BAND_ANGLE_DEG = 20.0  # fork angle to project around land

    # --- Spatial reference for lat/lon ---
    sr_wgs84 = arcpy.SpatialReference(4326)

    # --- Create output feature class ---
    # Put output in same workspace as input if path is a simple name
    if arcpy.Describe(out_fc).dataType in ["FeatureClass", "Feature Layer"]:
        # user gave full path into a GDB or feature dataset
        out_path = arcpy.Describe(out_fc).path
        out_name = arcpy.Describe(out_fc).name
    else:
        # safer generic split
        out_path, out_name = _split_path(out_fc)

    arcpy.AddMessage(f"Creating output feature class: {out_fc}")

    arcpy.management.CreateFeatureclass(
        out_path,
        out_name,
        "POINT",
        spatial_reference=sr_wgs84
    )

    # Add fields for context and QA
    arcpy.management.AddField(out_fc, "SrcOID", "LONG")
    arcpy.management.AddField(out_fc, "Heading", "DOUBLE")
    arcpy.management.AddField(out_fc, "Speed_kn", "DOUBLE")
    arcpy.management.AddField(out_fc, "Time_hr", "DOUBLE")
    arcpy.management.AddField(out_fc, "Dist_m", "DOUBLE")
    arcpy.management.AddField(out_fc, "OnLand", "SHORT")  # 0 = sea, 1 = land
    arcpy.management.AddField(out_fc, "Scenario", "TEXT", field_length=32)
    arcpy.management.AddField(out_fc, "LandDist_m", "DOUBLE")

    # Optional: store original lat/lon in attributes
    arcpy.management.AddField(out_fc, "Lat", "DOUBLE")
    arcpy.management.AddField(out_fc, "Lon", "DOUBLE")
    arcpy.management.AddField(out_fc, "PredTime", "DATE")

    # --- Insert cursor for output ---
    out_fields = [
        "SHAPE@",
        "SrcOID",
        "Heading",
        "Speed_kn",
        "Time_hr",
        "Dist_m",
        "OnLand",
        "Scenario",
        "LandDist_m",
        "Lat",
        "Lon",
        "PredTime",
    ]

    insert_cur = arcpy.da.InsertCursor(out_fc, out_fields)

    # --- Parse optional parameters ---
    time_baseline = None
    if time_baseline_txt:
        try:
            time_baseline = datetime.datetime.fromisoformat(time_baseline_txt)
        except Exception:
            arcpy.AddWarning(
                "Time baseline could not be parsed; predicted time will be empty."
            )

    if land_band_m:
        try:
            land_band_m_val = float(land_band_m)
        except Exception:
            land_band_m_val = DEFAULT_BAND_DISTANCE_M
            arcpy.AddWarning(
                "Land band distance not numeric; using default 1000 m."
            )
    else:
        land_band_m_val = DEFAULT_BAND_DISTANCE_M

    if band_count_txt:
        try:
            band_count = max(0, min(int(band_count_txt), 3))
        except Exception:
            band_count = 2
            arcpy.AddWarning(
                "Band count not numeric; using 2 forked scenarios."
            )
    else:
        band_count = 2

    # --- Read input and compute predicted positions ---
    in_fields = ["OID@", lat_field, lon_field, hdg_field, spd_field]

    with arcpy.da.SearchCursor(in_table, in_fields) as cur:
        for oid, lat, lon, heading_deg, speed_kn in cur:
            if lat is None or lon is None or heading_deg is None or speed_kn is None:
                # Skip incomplete records
                continue

            try:
                lat = float(lat)
                lon = float(lon)
                heading_deg = float(heading_deg)
                speed_kn = float(speed_kn)
            except Exception:
                # Skip if conversion fails
                continue

            # Distance traveled
            dist_m = speed_kn * time_hours * KNOT_TO_M

            # Convert nautical heading (0=N, clockwise) to ArcGIS angle
            # ArcGIS angle: 0 = East, 90 = North, CCW positive
            angle_arcgis = (90.0 - heading_deg) % 360.0

            # Build starting point
            start_pt = arcpy.Point(lon, lat)
            start_geom = arcpy.PointGeometry(start_pt, sr_wgs84)

            # Geodesic projection to predicted position
            try:
                pred_geom = start_geom.pointFromAngleAndDistance(
                    angle_arcgis,
                    dist_m,
                    "GEODESIC"
                )
            except Exception as ex:
                arcpy.AddWarning(
                    f"Failed to compute predicted point for OID {oid}: {ex}"
                )
                continue

            # Initially assume sea; we'll update OnLand later
            on_land = 0
            scenario = "center"
            land_dist = None
            predicted_time = None
            if time_baseline:
                predicted_time = time_baseline + datetime.timedelta(hours=time_hours)

            # Insert central prediction row
            insert_cur.insertRow([
                pred_geom,
                oid,
                heading_deg,
                speed_kn,
                time_hours,
                dist_m,
                on_land,
                scenario,
                land_dist,
                lat,
                lon,
                predicted_time,
            ])

            # Store inputs for optional banded scenarios
            if land_mask:
                _maybe_add_bands(
                    insert_cur,
                    start_geom,
                    angle_arcgis,
                    dist_m,
                    oid,
                    heading_deg,
                    speed_kn,
                    time_hours,
                    lat,
                    lon,
                    time_baseline,
                    band_count,
                    land_band_m_val,
                    DEFAULT_BAND_ANGLE_DEG,
                    sr_wgs84,
                )

    del insert_cur  # Release locks

    arcpy.AddMessage("Predicted positions created. Checking against land mask and proximity...")

    # --- Landmask integration: flag points that fall on land ---
    if land_mask:
        # Make feature layers
        arcpy.management.MakeFeatureLayer(out_fc, "lyr_predicted")
        arcpy.management.MakeFeatureLayer(land_mask, "lyr_land")

        # Select predicted points that intersect land polygons
        arcpy.management.SelectLayerByLocation(
            "lyr_predicted",
            "INTERSECT",
            "lyr_land",
            selection_type="NEW_SELECTION"
        )

        # Update OnLand = 1 for selected points
        with arcpy.da.UpdateCursor("lyr_predicted", ["OnLand"]) as ucur:
            for (on_land_val,) in ucur:
                ucur.updateRow((1,))

        # Compute near distances to land for all scenarios
        arcpy.analysis.Near(out_fc, land_mask)
        with arcpy.da.UpdateCursor(out_fc, ["NEAR_DIST", "LandDist_m"]) as ucur:
            for near_dist, _ in ucur:
                if near_dist is None:
                    ucur.updateRow((near_dist, None))
                else:
                    ucur.updateRow((near_dist, float(near_dist)))

        arcpy.AddMessage(
            "Landmask integration complete. 'OnLand' flagged and land proximity recorded."
        )

    else:
        arcpy.AddWarning(
            "No land mask provided; 'OnLand' field will remain 0 and proximity analysis will be skipped."
        )

    arcpy.AddMessage("PredictVesselPositions tool finished.")


def _split_path(full_path):
    """
    Simple helper to split a path into workspace and name.
    Works for file gdb paths and stand-alone feature classes.
    """
    # This is a fallback; often ArcGIS Describe(path).path/name is better.
    # But we use this for robustness when that isn't available.
    import os
    ws, name = os.path.split(full_path)
    if not ws:
        ws = arcpy.env.workspace
    return ws, name


def _maybe_add_bands(
    insert_cur,
    start_geom,
    angle_arcgis,
    dist_m,
    oid,
    heading_deg,
    speed_kn,
    time_hours,
    lat,
    lon,
    time_baseline,
    band_count,
    band_distance_m,
    band_angle_deg,
    spatial_ref,
):
    """Insert forked/banded predicted points around the land mask.

    Adds up to ``band_count`` alternate points (two by default) that are rotated
    left/right relative to the input heading. These are meant to represent
    plausible paths around nearby landmasses.
    """

    if band_count <= 0:
        return

    # Alternate angles: +/- band_angle_deg. For a third case, also project
    # directly ahead but offset by a land buffer distance.
    offsets = []
    offsets.append((-band_angle_deg, "band_left"))
    offsets.append((band_angle_deg, "band_right"))
    if band_count > 2:
        offsets.append((0.0, "band_center"))

    for angle_offset, scenario_label in offsets[:band_count]:
        try:
            band_geom = start_geom.pointFromAngleAndDistance(
                (angle_arcgis + angle_offset) % 360.0,
                dist_m + band_distance_m,
                "GEODESIC",
                spatial_ref,
            )
        except Exception as ex:
            arcpy.AddWarning(
                f"Failed to compute banded point for OID {oid} ({scenario_label}): {ex}"
            )
            continue

        predicted_time = None
        if time_baseline:
            predicted_time = time_baseline + datetime.timedelta(hours=time_hours)

        insert_cur.insertRow([
            band_geom,
            oid,
            heading_deg,
            speed_kn,
            time_hours,
            dist_m,
            0,  # OnLand default
            scenario_label,
            None,
            lat,
            lon,
            predicted_time,
        ])


if __name__ == "__main__":
    main()
