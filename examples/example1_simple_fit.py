import numpy as np
import lime

# Address of the Green Pea galaxy spectrum
gp_fits = './sample_data/gp121903_BR.fits'

# Load spectrum
wave, flux, header = lime.load_fits(gp_fits, instrument='OSIRIS')

# Galaxy redshift and the flux normalization
z_obj = 0.19531
norm_flux = 1e-15

# Line name and its location mask in the rest frame
lineLabel = 'H1_6563A_b'
lineWaves = np.array([6438.03, 6508.66, 6535.10, 6600.95, 6627.70, 6661.82])

# Fit configuration
fit_conf = {'H1_6563A_b': 'H1_6563A-N2_6584A-N2_6548A',
            'N2_6548A_amp': {'expr': 'N2_6584A_amp / 2.94'},
            'N2_6548A_kinem': 'N2_6584A'}

# Define a spectrum object
gp_spec = lime.Spectrum(wave, flux, redshift=z_obj, norm_flux=norm_flux)
gp_spec.plot_spectrum(frame='rest')

# Run the fit
gp_spec.fit_from_wavelengths(lineLabel, lineWaves)

# Show the results
gp_spec.display_results(show_fit_report=True, show_plot=True)

# Second attempt including the fit configuration
gp_spec.fit_from_wavelengths(lineLabel, lineWaves, fit_conf)
gp_spec.display_results(show_fit_report=True, show_plot=True, save_address=f'./{lineLabel}.png')

# Each fit is stored in the lines dataframe (linesDF) attribute
print(gp_spec.linesDF)

# It can be saved into different types of document using the command
lime.save_line_log(gp_spec.linesDF, 'example1_linelog', 'txt')
lime.save_line_log(gp_spec.linesDF, 'example1_linelog', 'fits')
lime.save_line_log(gp_spec.linesDF, 'example1_linelog', 'pdf')
lime.save_line_log(gp_spec.linesDF, 'example1_linelog', 'xlsx')


