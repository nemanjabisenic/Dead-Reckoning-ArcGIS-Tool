# -*- coding: utf-8 -*-
"""
PredictVesselPositions.py

ArcGIS Pro script tool that:
- Reads lat, lon, heading (deg), speed (knots)
- Uses a time interval (hours) to project positions geodesically
- Flags predicted positions that fall on landmasses (mask polygons)

Assumes input lat/lon are WGS 84 (EPSG:4326) or at least geographic degrees.
"""

import arcpy
import math


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

    # --- Constants ---
    KNOT_TO_M = 1852.0  # meters per knot-hour

    # --- Spatial reference for lat/lon ---
    sr_wgs84 = arcpy.SpatialReference(4326)

    # --- Create output feature class ---
    desc_in = arcpy.Describe(in_table)
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

    # Optional: store original lat/lon in attributes
    arcpy.management.AddField(out_fc, "Lat", "DOUBLE")
    arcpy.management.AddField(out_fc, "Lon", "DOUBLE")

    # --- Insert cursor for output ---
    out_fields = ["SHAPE@",
                  "SrcOID", "Heading", "Speed_kn",
                  "Time_hr", "Dist_m", "OnLand", "Lat", "Lon"]

    insert_cur = arcpy.da.InsertCursor(out_fc, out_fields)

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

            # Insert row
            insert_cur.insertRow([
                pred_geom,
                oid,
                heading_deg,
                speed_kn,
                time_hours,
                dist_m,
                on_land,
                lat,
                lon
            ])

    del insert_cur  # Release locks

    arcpy.AddMessage("Predicted positions created. Checking against land mask...")

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

        arcpy.AddMessage(
            "Landmask integration complete. 'OnLand' field set to 1 "
            "for predicted points intersecting land."
        )

    else:
        arcpy.AddWarning("No land mask provided; 'OnLand' field will remain 0 for all points.")

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


if __name__ == "__main__":
    main()
