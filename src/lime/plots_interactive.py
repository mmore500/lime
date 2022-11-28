import logging
import numpy as np
import pandas as pd

from pathlib import Path
from matplotlib import pyplot as plt, gridspec, patches, rc_context, cm, colors
from matplotlib.widgets import RadioButtons, SpanSelector
from astropy.io import fits

from .io import load_lines_log, save_line_log
from .plots import Plotter, frame_mask_switch_2, save_close_fig_swicth, _auto_flux_scale, STANDARD_PLOT, STANDARD_AXES, \
    determine_cube_images, load_spatial_masks, check_image_size, image_map_labels, image_plot, spec_plot, spatial_mask_plot, _masks_plot
from .tools import label_decomposition, blended_label_from_log, define_masks
from .transitions import Line
from astropy.table import Table


_logger = logging.getLogger('LiMe')


def check_previous_mask(input_mask, user_mask=None, wave_rest=None):

    # Add the lines from the input mask to the user mask and treat them as inactive
    if user_mask is not None:

        # Crop the input mask to exclude blended/merged lines in the previous mask
        idcs_comb = user_mask.index.str.endswith('_b') | user_mask.index.str.endswith('_m')
        comb_lines = user_mask.loc[idcs_comb].index.str[:-2]
        input_mask_crop = input_mask.loc[~input_mask.index.isin(comb_lines)]

        # Define for line status
        idcsNoMatch = ~input_mask_crop.index.isin(user_mask.index)
        active_lines = np.zeros(user_mask.index.size + np.sum(idcsNoMatch)).astype(bool)
        active_lines[:user_mask.index.size] = True

        # Join the lists and sort by wavelength
        user_mask = pd.concat([user_mask, input_mask_crop.loc[idcsNoMatch]])
        ion_array, wave_array, latex_array = label_decomposition(user_mask.index.values)
        idx_array = np.argsort(wave_array)
        user_mask = user_mask.iloc[idx_array]
        active_lines = active_lines[idx_array]

    # Use all mask and treat them as active
    else:
        user_mask = input_mask.copy()
        active_lines = np.ones(len(user_mask.index)).astype(bool)

    # Establish the lower and upper wavelength limits
    if np.ma.isMaskedArray(wave_rest):
        w_min, w_max = wave_rest.data[0], wave_rest.data[-1]
    else:
        w_min, w_max = wave_rest[0], wave_rest[-1]

    idx_rows_cont = (user_mask.w1 > w_min) & (user_mask.w6 < w_max)
    idx_row_line = (user_mask.w3 > w_min) & (user_mask.w4 < w_max)

    # Inform if one or more lines have been excluded from the interface
    if np.sum(idx_rows_cont) != np.sum(idx_row_line):

        output_message = ''
        range_lines = user_mask.loc[idx_row_line].index.values
        if user_mask.loc[range_lines[0]].w3 > w_min:
            output_message += f'\n-Transition {range_lines[0]} has been excluded from the inspection because its ' \
                              f'continuum is below the spectrum lower wavelength '
        if user_mask.loc[range_lines[1]].w4 < w_min:
            output_message += f'\n-Transition {range_lines[1]} has been excluded from the inspection because its ' \
                              f'continuum is above the spectrum higher wavelength '

    # Trim to the output lines
    user_mask = user_mask.loc[idx_rows_cont]
    active_lines = active_lines[idx_rows_cont]

    return user_mask, active_lines


def save_redshift_table(object, redshift, file_address):

    if redshift != 0:
        filePath = Path(file_address)

        if filePath.parent.is_dir():

            # Create a new dataframe and save it
            if not filePath.is_file():
                df = pd.DataFrame(data=redshift, index=[object], columns=['redshift'])

            # Replace or append to dataframe
            else:
                df = pd.read_csv(filePath, delim_whitespace=True, header=0, index_col=0)
                df.loc[object, 'redshift'] = redshift

            # Save back
            with open(filePath, 'wb') as output_file:
                string_DF = df.to_string()
                output_file.write(string_DF.encode('UTF-8'))

        else:
            _logger.warning(f'Output redshift table folder does not exist at {file_address}')

    return


def circle_band_label(current_label):
    line_suffix = current_label[-2:]

    if line_suffix == '_b':
        new_label = f'{current_label[:-2]}_m'
    elif line_suffix == '_m':
        new_label = current_label[:-2]
    else:
        new_label = f'{current_label}_b'

    return new_label


def save_or_clear_log(log, log_address, activeLines, log_parameters=['w1', 'w2', 'w3', 'w4', 'w5', 'w6'], input_log=None):

    if np.sum(activeLines) == 0:
        if log_address.is_file():
            log_address.unlink()
    else:
        if log_address is not None:
            save_line_log(log.loc[activeLines], log_address, parameters=log_parameters)
        else:
            _logger.warning(r"Not output redshift lob provided, the selection won't be stored")

    # Update the user input log to the new selection
    if input_log is not None:
        input_log = log.loc[activeLines]

    return


class BandsInspection:

    def __init__(self):

        self._y_scale = None
        self._log_address = None
        self._rest_frame = None
        self._activeLines = None
        self._lineList = None

        self.line = None
        self.mask = None
        self.log = None

        self._idx_ax = None
        self._color_bg = {True: 'white',
                          False: 'xkcd:salmon'}

        return

    def bands(self, input_mask, output_log_address=None, y_scale='auto', n_cols=6, n_rows=None, col_row_scale=(2, 1.5),
              rest_frame=True, maximize=False, plt_cfg={}, ax_cfg={}):

        """
        This class plots the masks from the ``_log_address`` as a grid for the input spectrum. Clicking and
        dragging the mouse within a line cell will update the line band region, both in the plot and the ``_log_address``
        file provided.

        Assuming that the band wavelengths `w1` and `w2` specify the adjacent blue (left continuum), the `w3` and `w4`
        wavelengths specify the line band and the `w5` and `w6` wavelengths specify the adjacent red (right continuum)
        the interactive selection has the following rules:

        * The plot wavelength range is always 5 pixels beyond the mask bands. Therefore dragging the mouse beyond the
          mask limits (below `w1` or above `w6`) will change the displayed range. This can be used to move beyond the
          original mask limits.

        * Selections between the `w2` and `w5` wavelength bands are always assigned to the line region mask as the new
          `w3` and `w4` values.

        * Due to the previous point, to increase the `w2` value or to decrease `w5` value the user must select a region
          between `w1` and `w3` or `w4` and `w6` respectively.

        The user can limit the number of lines displayed on the screen using the ``lines_interval`` parameter. This
        parameter can be an array of strings with the labels of the target lines or a two value integer array with the
        interval of lines to plot.

        Lines in the mask file outside the spectral wavelength range will be excluded from the plot: w2 and w5 smaller
        and greater than the blue and red wavelegnth values respectively.

        :param log_address: Address for the lines log mask file.
        :type log_address: str

        :param input_wave: Wavelength array of the input spectrum.
        :type input_wave: numpy.array

        :param input_flux: Flux array for the input spectrum.
        :type input_flux: numpy.array

        :param input_err: Sigma array of the `input_flux`
        :type input_err: numpy.array, optional

        :param redshift: Spectrum redshift
        :type redshift: float, optional

        :param norm_flux: Spectrum flux normalization
        :type norm_flux: float, optional

        :param crop_waves: Wavelength limits in a two value array
        :type crop_waves: np.array, optional

        :param n_cols: Number of columns of the grid plot
        :type n_cols: integer

        :param n_rows: Number of columns of the grid plot
        :type n_rows: integer

        :param lines_interval: List of lines or mask file line interval to display on the grid plot. In the later case
                               this interval must be a two value array.
        :type lines_interval: list

        :param y_scale: Y axis scale. The default value (auto) will switch between between linear and logarithmic scale
                        strong and weak lines respectively. Use ``linear`` and ``log`` for a fixed scale for all lines.
        :type y_scale: str, optional

        """

        # Assign the attribute values
        self._y_scale = y_scale
        self._log_address = None if output_log_address is None else Path(output_log_address)
        self._rest_frame = rest_frame

        # If provided, open the previous mask
        parent_mask = None
        if self._log_address is not None:
            if self._log_address.is_file():
                parent_mask = load_lines_log(self._log_address)

        # Establish the reference lines log to inspect the mask
        self.log, self._activeLines = check_previous_mask(input_mask, parent_mask, self._spec.wave_rest)

        # Proceed if there are lines in the mask for the object spectrum wavelength range
        if len(self.log.index) > 0:

            # Establish the initial list of lines
            self._lineList = self.log.index.values
            n_lines = self._lineList.size

            # Compute the number of rows configuration
            if n_lines > n_cols:
                if n_rows is None:
                    n_rows = int(np.ceil(n_lines / n_cols))
            else:
                n_cols, n_rows = n_lines, 1
            n_grid = n_cols * n_rows

            # Set the plot format where the user's overwrites the default
            default_fig_cfg = {'figure.figsize': (n_cols * col_row_scale[0], n_rows * col_row_scale[1]),
                               'axes.titlesize': 12}
            default_fig_cfg.update(plt_cfg)
            PLT_CONF, self._AXES_CONF = self._figure_format(default_fig_cfg, ax_cfg, norm_flux=self._spec.norm_flux,
                                                            units_wave=self._spec.units_wave,
                                                            units_flux=self._spec.units_flux)
            self._AXES_CONF.pop('xlabel')

            # Launch the interative figure
            with rc_context(PLT_CONF):

                # Figure attributes
                self._fig, ax = plt.subplots(nrows=n_rows, ncols=n_cols)
                ax_list = ax.flatten() if n_lines > 1 else [ax]

                # Generate plot
                spanSelectDict = {}
                for i in range(n_grid):
                    if i < n_lines:
                        self.line = self._lineList[i]
                        self.mask = self.log.loc[self.line, 'w1':'w6'].values
                        self._plot_line_BI(ax_list[i], self.line, self._rest_frame, self._y_scale)
                        spanSelectDict[f'spanner_{i}'] = SpanSelector(ax_list[i],
                                                                      self._on_select_MI,
                                                                      'horizontal',
                                                                      useblit=True,
                                                                      rectprops=dict(alpha=0.5, facecolor='tab:blue'),
                                                                      button=1)
                    else:
                        # Clear not filled axes
                        self._fig.delaxes(ax_list[i])

                # Connecting the figure to the interactive widgets
                self._fig.canvas.mpl_connect('button_press_event', self._on_click_MI)
                self._fig.canvas.mpl_connect('axes_enter_event', self._on_enter_axes_MI)

                # Show the image
                save_close_fig_swicth(None, 'tight', self._fig, maximize=maximize)

        else:
            _logger.warning(f'No lines found in the lines mask for the object wavelentgh range')

            return

        return self.log.loc[self._activeLines]

    def _plot_line_BI(self, ax, line, frame, y_scale='auto'):

        if self.mask.size == 6:

            # Background for selective line for selected lines
            active_check = self._activeLines[self._lineList == line][0]
            ax.set_facecolor(self._color_bg[active_check])

            # Check components
            blended_check, profile_label = blended_label_from_log(line, self.log)
            list_comps = profile_label.split('-') if blended_check else [line]

            # Reference _frame for the plot
            wave_plot, flux_plot, z_corr, idcs_mask = frame_mask_switch_2(self._spec.wave, self._spec.flux,
                                                                          self._spec.redshift, frame)

            # Establish the limits for the line spectrum plot
            mask = self.log.loc[list_comps[0], 'w1':'w6'] * (1 + self._spec.redshift)
            idcsM = np.searchsorted(wave_plot, mask)
            idxL = idcsM[0] - 5 if idcsM[0] > 5 else idcsM[0]
            idxH = idcsM[-1] + 5 if idcsM[-1] < idcsM[-1] + 5 else idcsM[-1]

            # Plot the spectrum
            ax.step(wave_plot[idxL:idxH]/z_corr, flux_plot[idxL:idxH]*z_corr, where='mid', color=self._color_dict['fg'])

            # Continuum bands
            self._bands_plot(ax, wave_plot, flux_plot, z_corr, idcsM, line)

            # Plot the masked pixels
            _masks_plot(ax, [line], wave_plot[idxL:idxH], flux_plot[idxL:idxH], z_corr, self.log, idcs_mask[idxL:idxH])

            # Formatting the figure
            ax.yaxis.set_major_locator(plt.NullLocator())
            ax.xaxis.set_major_locator(plt.NullLocator())

            ax.update({'title': line})
            ax.yaxis.set_ticklabels([])
            ax.axes.yaxis.set_visible(False)

            # Scale each
            _auto_flux_scale(ax, flux_plot[idxL:idxH]*z_corr, y_scale)

            return

    def _on_select_MI(self, w_low, w_high):

        # Check we are not just clicking on the plot
        if w_low != w_high:

            # Convert the wavelengths to the rest frame if necessary
            if self._rest_frame is False:
                w_low, w_high = w_low/(1 + self._spec.redshift), w_high/(1 +  self._spec.redshift)

            # Case we have all selections
            if self.mask.size == 6:

                # Correcting line band
                if w_low > self.mask[1] and w_high < self.mask[4]:
                    self.mask[2] = w_low
                    self.mask[3] = w_high

                # Correcting blue band
                elif w_low < self.mask[2] and w_high < self.mask[2]:
                    self.mask[0] = w_low
                    self.mask[1] = w_high

                # Correcting Red
                elif w_low > self.mask[3] and w_high > self.mask[3]:
                    self.mask[4] = w_low
                    self.mask[5] = w_high

                # Removing line
                elif w_low < self.mask[0] and w_high > self.mask[5]:
                    print(f'\n-- The line {self.line} mask has been removed')

                # Weird case
                else:
                    _logger.info(f'Unsuccessful line selection: {self.line}: w_low: {w_low}, w_high: {w_high}')

            # Save the new selection to the lines log
            self.log.loc[self.line, 'w1':'w6'] = self.mask

            # Save the log to the file
            save_or_clear_log(self.log, self._log_address, self._activeLines)

            # Redraw the line measurement
            self._ax.clear()
            self._plot_line_BI(self._ax, self.line, frame=self._rest_frame, y_scale=self._y_scale)
            self._fig.canvas.draw()

        return

    def _on_enter_axes_MI(self, event):

        # Assign current line and axis
        self._ax = event.inaxes
        self.line = self._ax.get_title()
        self._idx_ax = np.where(self._lineList == self.line)
        self.mask = self.log.loc[self.line, 'w1':'w6'].values

    def _on_click_MI(self, event):

        if event.button in (2, 3):

            # Update the line label
            if event.button == 2:

                # Update the new line name
                current_label = self._lineList[self._idx_ax][0]
                self.line = circle_band_label(current_label)
                self.log.rename(index={current_label: self.line}, inplace=True)
                self._lineList = self.log.index.values

            # Update the line active status
            else:

                # Invert the line type
                self._activeLines[self._idx_ax] = np.invert(self._activeLines[self._idx_ax])

            # Save the log to the file
            save_or_clear_log(self.log, self._log_address, self._activeLines)

            # Plot the line selection with the new Background
            self._ax.clear()
            self._plot_line_BI(self._ax, self.line, self._rest_frame, self._y_scale)
            self._fig.canvas.draw()

        return


class RedshiftInspection:

    def __init__(self):

        # Attributes
        self._label = None
        self._log_address = None
        self._visits_array = None
        self._lineWave = None
        self._lineList = None
        self._AXES_CONF = None
        self._redshift_pred = None
        self._user_point = None

    def redshift(self, obj_reference, reference_lines, output_file=None, plt_cfg={}, ax_cfg={}, visits=None, in_fig=None,
                 in_axis=None):

        self._spec_name = obj_reference
        self._log_address = output_file
        self._visits_array = visits

        # Estate the line labels for the plot
        ion_array, self._lineWave, self._lineList = label_decomposition(reference_lines)
        idcs_sorted = np.argsort(self._lineWave)
        self._lineWave, self._lineList = self._lineWave[idcs_sorted], self._lineList[idcs_sorted]

        # Set figure format with the user inputs overwriting the default conf
        legend_check = True if obj_reference is not None else False
        plt_cfg.setdefault('figure.figsize', (10, 6))
        PLT_CONF, self._AXES_CONF = self._figure_format(plt_cfg, ax_cfg, norm_flux=self._spec.norm_flux,
                                                  units_wave=self._spec.units_wave, units_flux=self._spec.units_flux)

        # Create and fill the figure
        with rc_context(PLT_CONF):

            # Generate the figure object and figures
            self._fig = plt.figure() if in_fig is None else in_fig
            gs = gridspec.GridSpec(nrows=1, ncols=2, figure=self._fig, width_ratios=[2, 0.5], height_ratios=[1])
            self._ax = self._fig.add_subplot(gs[0])
            self._ax.set(**self._AXES_CONF)

            # Try to center the image
            # maximize_center_matplotlib_fig()

            # Line Selection axis
            buttoms_ax = self._fig.add_subplot(gs[1])
            buttons_list = [r'$None$'] + list(self._lineList) + [r'$Unknown$']
            radio = RadioButtons(buttoms_ax, buttons_list)
            for circle in radio.circles:  # Make the buttons a bit rounder
                circle.set_height(0.025)
                circle.set_width(0.075)

            # Plot the spectrum
            self._plot_spectrum_ZI(self._ax, spec_label=obj_reference)

            # Connect the widgets
            radio.on_clicked(self._button_ZI)
            self._fig.canvas.mpl_connect('button_press_event', self._on_click_ZI)

            # Plot on screen unless an output address is provided
            save_close_fig_swicth(None, 'tight', self._fig)

    def _plot_spectrum_ZI(self, ax, log_scale=False, spec_label='Object spectrum', frame='observed'):

        # Reference _frame for the plot
        wave_plot, flux_plot, z_corr, idcs_mask = frame_mask_switch_2(self._spec.wave, self._spec.flux,
                                                                      self._spec.redshift, frame)

        # Plot the spectrum
        ax.step(wave_plot / z_corr, flux_plot * z_corr, label=spec_label, where='mid', color=self._color_dict['fg'])

        # Plot the masked pixels
        self._masks_plot(ax, None, wave_plot, flux_plot, z_corr, self._spec.log, idcs_mask)

        # Switch y_axis to logarithmic scale if requested
        if log_scale:
            ax.set_yscale('log')

        return

    def _plot_line_labels_ZI(self, ax, click_coord, redshift_pred):

        if click_coord is not None:
            ax.scatter(click_coord[0], click_coord[1], s=20, marker=r'o', color=self._color_dict['error'])

        if redshift_pred is not None:
            if not np.isnan(redshift_pred):

                # Remove mask for better limit plotting
                if np.ma.isMaskedArray(self._spec.wave):
                    wave_plot, flux_plot = self._spec.wave.data, self._spec.flux.data
                else:
                    wave_plot, flux_plot = self._spec.wave, self._spec.flux

                idcs_in_range = np.logical_and(self._lineWave*(1 + self._redshift_pred) >= wave_plot[0],
                                               self._lineWave*(1 + self._redshift_pred) <= wave_plot[-1])
                linesRange = self._lineWave[idcs_in_range]

                idx_in_spec = np.searchsorted(wave_plot, linesRange*(1 + self._redshift_pred))

                for i, lineWave in enumerate(linesRange):
                    ax.annotate(self._lineList[idcs_in_range][i],
                                xy=(wave_plot[idx_in_spec][i], flux_plot[idx_in_spec][i]),
                                xytext=(wave_plot[idx_in_spec][i], 0.80),
                                horizontalalignment="center",
                                rotation=90,
                                xycoords='data', textcoords=("data", "axes fraction"),
                                arrowprops=dict(arrowstyle="->"))

        return

    def _launch_plots_ZI(self):

        # Compute the new redshift
        if self._ref_wave is None or self._user_point is None:
            self._redshift_pred = 0
        else:
            self._redshift_pred = self._user_point[0] / self._ref_wave - 1

        # Store the figure limits
        xlim, ylim = self._ax.get_xlim(), self._ax.get_ylim()

        # Redraw the figure
        self._ax.clear()
        self._plot_spectrum_ZI(self._ax, log_scale=False, spec_label=self._spec_name)
        self._plot_line_labels_ZI(self._ax, self._user_point, self._redshift_pred)
        self._ax.set_xlim(xlim)
        self._ax.set_ylim(ylim)
        self._ax.set(**self._AXES_CONF)
        self._fig.canvas.draw()

        # Save to database if provided
        save_redshift_table(self._spec_name, self._redshift_pred, self._log_address)

    def _button_ZI(self, line_selection):

        # Confirm the input line
        if line_selection not in [f'$None$', r'$Unknown$']:
            idx_line = self._lineList == line_selection
            self.line = line_selection
            self._ref_wave = self._lineWave[idx_line][0]
        else:
            self.line = None
            self._ref_wave = 0 if line_selection is f'$None$' else np.nan

        # Replot the figure
        self._launch_plots_ZI()

        return

    def _on_click_ZI(self, event, tolerance=3):

        if event.button == 3:
            idx_selec = np.searchsorted(self._spec.wave, event.xdata)
            idx_max = idx_selec + np.argmax(self._spec.flux[idx_selec-tolerance:idx_selec+tolerance]) - tolerance
            self._user_point = (self._spec.wave[idx_max], self._spec.flux[idx_max])

            # Replot the figure
            self._launch_plots_ZI()

        return


class CubeInspector:

    def __init__(self):

        # Data attributes
        self.grid_mesh = None
        self.bg_image = None
        self.fg_image = None
        self.fg_levels = None
        self.hdul_linelog = None
        self.ext_log = None

        # Mask correction attributes
        self.mask_file = None
        self.mask_ext = None
        self.masks_dict = {}
        self.mask_color = None
        self.mask_array = None

        # Plot attributes
        self._ax0, self._ax1, self._ax2, self.in_ax = None, None, None, None
        self.fig_conf = None
        self.axes_conf = {}
        self.axlim_dict = {}
        self.color_norm = None
        self.mask_color_i = None
        self.key_coords = None
        self.marker = None
        self.rest_frame = None
        self.log_scale = None

        return

    def cube(self, line, band=None, percentil_bg=60, line_fg=None, band_fg=None, percentils_fg=[90, 95, 99], bands_frame=None,
             bg_scale=None, fg_scale=None, bg_color='gray', fg_color='viridis', mask_color='viridis_r', mask_alpha=0.2,
             wcs=None, plt_cfg={}, ax_cfg_image={}, ax_cfg_spec={}, title=None, masks_file=None, lines_log_address=None,
             maximise=False, rest_frame=False, log_scale=False):

        # Prepare the background image data
        line_bg, self.bg_image, self.bg_levels, self.bg_scale = determine_cube_images(self._cube, line, band, bands_frame,
                                                                       percentil_bg, bg_scale, contours_check=False)

        # Prepare the foreground image data
        line_fg, self.fg_image, self.fg_levels, self.fg_scale = determine_cube_images(self._cube, line_fg, band_fg, bands_frame,
                                                                       percentils_fg, fg_scale, contours_check=True)

        # Mesh for the countours
        if line_fg is not None:
            y, x = np.arange(0, self.fg_image.shape[0]), np.arange(0, self.fg_image.shape[1])
            self.fg_mesh = np.meshgrid(x, y)
        else:
            self.fg_mesh = None

        # Colors
        self.bg_color, self.fg_color, self.mask_color, self.mask_alpha = bg_color, fg_color, mask_color, mask_alpha

        # Frame
        self.rest_frame, self.log_scale = rest_frame, log_scale

        # Load the masks
        self.masks_dict = load_spatial_masks(masks_file)

        # Check that the images have the same size
        check_image_size(self.bg_image, self.fg_image, self.masks_dict)

        # Image mesh grid
        frame_size = self._cube.flux.shape
        y, x = np.arange(0, frame_size[1]), np.arange(0, frame_size[2])
        self.grid_mesh = np.meshgrid(x, y)

        # Use central voxel as initial coordinate
        self.key_coords = int(self._cube.flux.shape[1]/2), int(self._cube.flux.shape[2]/2)

        if len(self.masks_dict) > 0:
            self.mask_ext = list(self.masks_dict.keys())[0]
        else:
            self.mask_ext = '_LINESLOG'

        # Load the complete fits lines log if input
        if lines_log_address is not None:
            self.hdul_linelog = fits.open(lines_log_address, lazy_load_hdus=False)

        # State the plot labelling
        title, x_label, y_label = image_map_labels(title, wcs, line_bg, line_fg, self.masks_dict)

        # User configuration overwrite default figure format
        plt_cfg.setdefault('figure.figsize', (10, 5))
        plt_cfg.setdefault('axes.titlesize', 12)
        plt_cfg.setdefault('legend.fontsize', 10)
        self.fig_conf, ax_cfg_spec = self._figure_format(plt_cfg, ax_cfg_spec, norm_flux=self._cube.norm_flux,
                                                         units_wave=self._cube.units_wave, units_flux=self._cube.units_flux)

        # Image axes format
        ax_cfg_image.setdefault('xlabel', x_label)
        ax_cfg_image.setdefault('ylabel', y_label)
        ax_cfg_image.setdefault('title', title)

        # Container for both axes format
        self.axes_conf = {'image': ax_cfg_image, 'spectrum': ax_cfg_spec}

        # Create the figure
        with rc_context(self.fig_conf):

            # Figure structure
            self._fig = plt.figure()
            gs = gridspec.GridSpec(nrows=1, ncols=2, figure=self._fig, width_ratios=[1, 2], height_ratios=[1])

            # Create subgrid for buttons if mask file provided
            if len(self.masks_dict) > 0:
                gs_image = gridspec.GridSpecFromSubplotSpec(nrows=2, ncols=1, subplot_spec=gs[0], height_ratios=[0.8, 0.2])
            else:
                gs_image = gs

            # Image axes Astronomical coordinates if provided
            if wcs is None:
                self._ax0 = self._fig.add_subplot(gs_image[0])
            else:
                self._ax0 = self._fig.add_subplot(gs_image[0], projection=wcs, slices=('x', 'y', 1))

            # Spectrum plot
            self._ax1 = self._fig.add_subplot(gs[1])

            # Buttons axis if provided
            if len(self.masks_dict) > 0:
                self._ax2 = self._fig.add_subplot(gs_image[1])
                radio = RadioButtons(self._ax2, list(self.masks_dict.keys()))

            # Load the complete fits lines log if input
            if lines_log_address is not None:
                self.hdul_linelog = fits.open(lines_log_address, lazy_load_hdus=False)

            # Plot the data
            self.data_plots()

            # Connect the widgets
            self._fig.canvas.mpl_connect('button_press_event', self.on_click)
            self._fig.canvas.mpl_connect('axes_enter_event', self.on_enter_axes)
            if len(self.masks_dict) > 0:
                radio.on_clicked(self.mask_selection)

            # Display the figure
            save_close_fig_swicth()

            # Close the lines log if it has been opened
            if isinstance(self.hdul_linelog, fits.hdu.HDUList):
                self.hdul_linelog.close()

        return

    def data_plots(self):

        # Delete previous marker
        if self.marker is not None:
            self.marker.remove()
            self.marker = None

        # Background image
        self.im, _, self.marker = image_plot(self._ax0, self.bg_image, self.fg_image, self.fg_levels, self.fg_mesh,
                                        self.bg_scale, self.fg_scale, self.bg_color, self.fg_color, self.key_coords)

        # Spatial masks
        spatial_mask_plot(self._ax0, self.masks_dict, self.mask_color, self.mask_alpha, self._cube.units_flux,
                          mask_list=[self.mask_ext])

        # Voxel spectrum
        if self.key_coords is not None:
            idx_j, idx_i = self.key_coords
            flux_voxel = self._cube.flux[:, idx_j, idx_i]
            log = None

            # Check if lines have been measured
            if self.hdul_linelog is not None:
                ext_name = f'{idx_j}-{idx_i}_LINESLOG'#{self.ext_log}'

                # Better sorry than permission. Faster?
                try:
                    lineslogDF = Table.read(self.hdul_linelog[ext_name]).to_pandas()
                    lineslogDF.set_index('index', inplace=True)
                    log = lineslogDF
                except KeyError:
                    _logger.info(f'Extension {ext_name} not found in the input file')

            # Plot spectrum
            spec_plot(self._ax1, self._cube.wave, flux_voxel, self._cube.redshift, self._cube.norm_flux,
                      rest_frame=self.rest_frame, log=log, units_wave=self._cube.units_wave,
                      units_flux=self._cube.units_flux, color_dict=self._color_dict)

            if self.log_scale:
                self._ax.set_yscale('log')

        # Update the axis
        self.axes_conf['spectrum']['title'] = f'Voxel {idx_j} - {idx_i}'
        self._ax0.update(self.axes_conf['image'])
        self._ax1.update(self.axes_conf['spectrum'])

        return

    def on_click(self, event, new_voxel_button=3, mask_button='m'):

        if self.in_ax == self._ax0:

            # Save axes zoom
            self.save_zoom()

            if event.button == new_voxel_button:

                # Save clicked coordinates for next plot
                self.key_coords = np.rint(event.ydata).astype(int), np.rint(event.xdata).astype(int)

                # Remake the drawing
                self.im.remove()# self.ax0.clear()
                self._ax1.clear()
                self.data_plots()

                # Reset the image
                self.reset_zoom()
                self._fig.canvas.draw()

            if event.dblclick:
                if len(self.masks_dict) > 0:

                    # Save clicked coordinates for next plot
                    self.key_coords = np.rint(event.ydata).astype(int), np.rint(event.xdata).astype(int)

                    # Add or remove voxel from mask:
                    self.spaxel_selection()

                    # Save the new mask: # TODO just update the one we need
                    hdul = fits.HDUList([fits.PrimaryHDU()])
                    for mask_name, mask_attr in self.masks_dict.items():
                        hdul.append(fits.ImageHDU(name=mask_name, data=mask_attr[0].astype(int), ver=1, header=mask_attr[1]))
                    hdul.writeto(self.mask_file, overwrite=True, output_verify='fix')

                    # Remake the drawing
                    self.im.remove()
                    self._ax1.clear()
                    self.data_plots()

                    # Reset the image
                    self.reset_zoom()
                    self._fig.canvas.draw()

            return

    def mask_selection(self, mask_label):

        # Assign the mask
        self.mask_ext = mask_label

        # Replot the figure
        self.save_zoom()
        self.im.remove()
        self._ax1.clear()
        self.data_plots()

        # Reset the image
        self.reset_zoom()
        self._fig.canvas.draw()

        return

    def spaxel_selection(self):

        for mask, mask_data in self.masks_dict.items():
            mask_matrix = mask_data[0]
            if mask == self.mask_ext:
                mask_matrix[self.key_coords[0], self.key_coords[1]] = not mask_matrix[self.key_coords[0], self.key_coords[1]]
            else:
                mask_matrix[self.key_coords[0], self.key_coords[1]] = False

        return

    def on_enter_axes(self, event):
        self.in_ax = event.inaxes

    def save_zoom(self):
        self.axlim_dict['image_xlim'] = self._ax0.get_xlim()
        self.axlim_dict['image_ylim'] = self._ax0.get_ylim()
        self.axlim_dict['spec_xlim'] = self._ax1.get_xlim()
        self.axlim_dict['spec_ylim'] = self._ax1.get_ylim()

        return

    def reset_zoom(self):

        self._ax0.set_xlim(self.axlim_dict['image_xlim'])
        self._ax0.set_ylim(self.axlim_dict['image_ylim'])
        self._ax1.set_xlim(self.axlim_dict['spec_xlim'])
        self._ax1.set_ylim(self.axlim_dict['spec_ylim'])

        return


class SpectrumCheck(Plotter, RedshiftInspection, BandsInspection):

    def __init__(self, spectrum):

        # Instantiate the dependencies
        Plotter.__init__(self)
        RedshiftInspection.__init__(self)
        BandsInspection.__init__(self)

        # Lime spectrum object with the scientific data
        self._spec = spectrum

        # Variables for the matplotlib figures
        self._fig, self._ax = None, None

        return


class CubeCheck(Plotter, CubeInspector):

    def __init__(self, cube):

        # Instantiate the dependencies
        Plotter.__init__(self)
        CubeInspector.__init__(self)

        # Lime cube object with the scientific data
        self._cube = cube

        # Variables for the matplotlib figures
        self._fig, self._ax = None, None

        return