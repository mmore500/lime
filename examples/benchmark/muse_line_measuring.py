from pathlib import Path
from mpdaf.obj import Cube
from astropy.wcs import WCS
import numpy as np
import lime


def read_muse_cube(file_address):

    cube_obj = Cube(filename=str(file_address))
    header = cube_obj.data_header

    dw = header['CD3_3']
    w_min = header['CRVAL3']
    nPixels = header['NAXIS3']
    w_max = w_min + dw * nPixels
    wave_array = np.linspace(w_min, w_max, nPixels, endpoint=False)

    return wave_array, cube_obj, header


# Inputs
cfg_file = 'muse.toml'
mask_file = 'CGCG007_masks.fits'
cube_file = '../benchmark/CGCG007.fits'

# Outputs
log_file = 'log_CGCG007.fits'

# Load configuration
cfg = lime.load_cfg(cfg_file)
norm_flux = cfg['sample_data']['norm_flux']
z_obj = cfg['sample_data']['redshift']

# Load cube
wave_array, cube, hdr = read_muse_cube(cube_file)
flux_cube = cube.data.data * norm_flux
err_cube = np.sqrt(cube.var.data) * norm_flux
mask_pixel_cube = np.isnan(flux_cube)
wcs = WCS(hdr)

# Create MUSE
cgcg007 = lime.Cube(wave_array, flux_cube, err_cube, redshift=z_obj, norm_flux=norm_flux,
                    wcs=wcs, pixel_mask=mask_pixel_cube)

# Perform the measurements
cgcg007.fit.spatial_mask(mask_file, line_detection=True, output_address=log_file, fit_conf=cfg)