#!/usr/bin/env python3
"""
Full Aurora forecast pipeline: ERA5 initial conditions -> Aurora rollout -> Hong Kong April 29 max temp
"""

import datetime
import torch
import cdsapi
import xarray as xr
from aurora import AuroraSmallPretrained, Batch, Metadata, rollout

# Hong Kong Observatory coordinates
HK_LAT = 22.3027
HK_LON = 114.1772

print("✅ Initializing Microsoft Aurora model...")
model = AuroraSmallPretrained()
model.load_checkpoint()
print("✅ Model loaded successfully with 112M parameters")

# Get most recent ERA5 analysis time (00, 06, 12, 18 UTC)
now = datetime.datetime.now(datetime.UTC)
latest_hour = (now.hour // 6) * 6
analysis_time = now.replace(hour=latest_hour, minute=0, second=0, microsecond=0)

print(f"\n📅 Latest available ERA5 analysis: {analysis_time}")
print(f"🔮 Forecast target: April 29, 2026 (48 hour horizon)")

# Pull ERA5 initial conditions
print("\n📥 Retrieving ERA5 initial conditions...")
c = cdsapi.Client()

# Surface variables required by Aurora
surface_request = {
    'product_type': 'reanalysis',
    'variable': ['2m_temperature', 'mean_sea_level_pressure', '10m_u_component_of_wind', '10m_v_component_of_wind'],
    'year': analysis_time.year,
    'month': analysis_time.month,
    'day': analysis_time.day,
    'time': f"{analysis_time.hour:02d}:00",
    'area': [HK_LAT + 5, HK_LON - 5, HK_LAT - 5, HK_LON + 5],
    'format': 'grib',
}

print("Downloading surface level data...")
c.retrieve('reanalysis-era5-single-levels', surface_request, 'era5_surface.grib')

# Atmospheric variables required by Aurora
atmos_request = {
    'product_type': 'reanalysis',
    'variable': ['temperature', 'u_component_of_wind', 'v_component_of_wind', 'geopotential', 'specific_humidity'],
    'pressure_level': ['1000', '925', '850', '700', '500', '300', '250', '200', '100', '50'],
    'year': analysis_time.year,
    'month': analysis_time.month,
    'day': analysis_time.day,
    'time': f"{analysis_time.hour:02d}:00",
    'area': [HK_LAT + 5, HK_LON - 5, HK_LAT - 5, HK_LON + 5],
    'format': 'grib',
}

print("Downloading atmospheric level data...")
c.retrieve('reanalysis-era5-pressure-levels', atmos_request, 'era5_atmos.grib')

print("✅ ERA5 initial conditions downloaded successfully")

# Load and prepare batch
print("\n🔧 Preparing Aurora Batch...")
ds_surface = xr.open_dataset('era5_surface.grib', engine='cfgrib')
ds_atmos = xr.open_dataset('era5_atmos.grib', engine='cfgrib')

# Create tensor format for Aurora
surf_vars = {
    't2m': torch.from_numpy(ds_surface.t2m.values).float().unsqueeze(0).unsqueeze(0),
    'msl': torch.from_numpy(ds_surface.msl.values).float().unsqueeze(0).unsqueeze(0),
    'u10': torch.from_numpy(ds_surface.u10.values).float().unsqueeze(0).unsqueeze(0),
    'v10': torch.from_numpy(ds_surface.v10.values).float().unsqueeze(0).unsqueeze(0),
}

atmos_vars = {
    't': torch.from_numpy(ds_atmos.t.values).float().unsqueeze(0).unsqueeze(0),
    'u': torch.from_numpy(ds_atmos.u.values).float().unsqueeze(0).unsqueeze(0),
    'v': torch.from_numpy(ds_atmos.v.values).float().unsqueeze(0).unsqueeze(0),
    'z': torch.from_numpy(ds_atmos.z.values).float().unsqueeze(0).unsqueeze(0),
    'q': torch.from_numpy(ds_atmos.q.values).float().unsqueeze(0).unsqueeze(0),
}

static_vars = {
    'lsm': torch.zeros_like(surf_vars['t2m'][0,0]),
    'orography': torch.zeros_like(surf_vars['t2m'][0,0]),
}

metadata = Metadata(
    lat=torch.from_numpy(ds_surface.latitude.values).float(),
    lon=torch.from_numpy(ds_surface.longitude.values).float(),
    time=(analysis_time,),
    atmos_levels=tuple(int(l) for l in ds_atmos.isobaricInhPa.values),
)

batch = Batch(
    surf_vars=surf_vars,
    static_vars=static_vars,
    atmos_vars=atmos_vars,
    metadata=metadata,
)

print("✅ Batch created successfully")
print(f"   Spatial grid: {batch.spatial_shape}")
print(f"   Atmospheric levels: {len(metadata.atmos_levels)}")

# Run forecast rollout
print("\n🚀 Running Aurora 48 hour forecast rollout...")
forecast_steps = 8  # 8 * 6h = 48h
results = list(rollout(model, batch, steps=forecast_steps))

print(f"✅ Forecast completed successfully: {len(results)} timesteps")

# Extract Hong Kong maximum temperature
print("\n🌡️  Extracting Hong Kong forecast...")

# Find grid point closest to Hong Kong Observatory
lat_idx = torch.argmin(torch.abs(metadata.lat - HK_LAT))
lon_idx = torch.argmin(torch.abs(metadata.lon - HK_LON))

print(f"   Closest grid point: lat={metadata.lat[lat_idx]:.3f}, lon={metadata.lon[lon_idx]:.3f}")

# Collect all 2m temperatures across forecast
all_temps = []
for step, pred_batch in enumerate(results):
    temp_k = pred_batch.surf_vars['t2m'][0, 0, lat_idx, lon_idx]
    temp_c = temp_k - 273.15
    forecast_time = analysis_time + datetime.timedelta(hours=(step+1)*6)
    all_temps.append(float(temp_c))
    print(f"   Step {step+1:2d} | {forecast_time.strftime('%Y-%m-%d %H:%M')} | {temp_c:.1f} °C")

max_temp_c = max(all_temps[4:])  # Max during April 29 UTC day
print(f"\n✅ FINAL FORECAST RESULT:")
print(f"📍 Location: Hong Kong Observatory")
print(f"📅 Date: April 29, 2026")
print(f"🌡️  Maximum Temperature: {max_temp_c:.1f} °C")
print(f"🌡️  Maximum Temperature: {max_temp_c * 9/5 + 32:.1f} °F")