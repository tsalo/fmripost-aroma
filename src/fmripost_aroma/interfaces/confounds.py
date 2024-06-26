"""Handling confounds."""

import os

import numpy as np
import pandas as pd
from nipype.interfaces.base import (
    BaseInterfaceInputSpec,
    File,
    SimpleInterface,
    TraitedSpec,
    traits,
)
from scipy import stats

from fmripost_aroma import config


class _ICAConfoundsInputSpec(BaseInterfaceInputSpec):
    mixing = File(exists=True, desc='melodic mixing matrix')
    aroma_features = File(exists=True, desc='output confounds file extracted from ICA-AROMA')
    skip_vols = traits.Int(desc='number of non steady state volumes identified')
    err_on_aroma_warn = traits.Bool(False, usedefault=True, desc='raise error if aroma fails')


class _ICAConfoundsOutputSpec(TraitedSpec):
    aroma_confounds = traits.Either(
        None, File(exists=True, desc='output confounds file extracted from ICA-AROMA')
    )
    mixing = File(exists=True, desc='melodic mix file with skip_vols added back')


class ICAConfounds(SimpleInterface):
    """Extract confounds from ICA-AROMA result directory."""

    input_spec = _ICAConfoundsInputSpec
    output_spec = _ICAConfoundsOutputSpec

    def _run_interface(self, runtime):
        aroma_confounds, mixing = _get_ica_confounds(
            mixing=self.inputs.mixing,
            aroma_features=self.inputs.aroma_features,
            skip_vols=self.inputs.skip_vols,
            newpath=runtime.cwd,
        )

        if self.inputs.err_on_aroma_warn and aroma_confounds is None:
            raise RuntimeError('ICA-AROMA failed')

        self._results['aroma_confounds'] = aroma_confounds
        self._results['mixing'] = mixing
        return runtime


def _get_ica_confounds(mixing, aroma_features, skip_vols, newpath=None):
    """Extract confounds from ICA-AROMA result directory.

    This function does the following:

    1. Add the number of non steady state volumes to the melodic mix file.
    2. Select motion ICs from the mixing matrix for new "confounds" file.
    """
    if newpath is None:
        newpath = os.getcwd()

    # Load input files
    aroma_features_df = pd.read_table(aroma_features)
    motion_ics = aroma_features_df.loc[
        aroma_features_df['classification'] == 'rejected'
    ].index.values
    mixing_arr = np.loadtxt(mixing, ndmin=2)

    # Prepare output paths
    mixing_out = os.path.join(newpath, 'mixing.tsv')
    aroma_confounds = os.path.join(newpath, 'AROMAAggrCompAROMAConfounds.tsv')

    # pad mixing_arr with rows of zeros corresponding to number non steady-state volumes
    if skip_vols > 0:
        zeros = np.zeros([skip_vols, mixing_arr.shape[1]])
        mixing_arr = np.vstack([zeros, mixing_arr])

    # save mixing_arr
    np.savetxt(mixing_out, mixing_arr, delimiter='\t')

    # Return dummy list of ones if no noise components were found
    if motion_ics.size == 0:
        config.loggers.interfaces.warning('No noise components were classified')
        return None, mixing_out

    # return dummy lists of zeros if no signal components were found
    good_ic_arr = np.delete(mixing_arr, motion_ics, 1).T
    if good_ic_arr.size == 0:
        config.loggers.interfaces.warning('No signal components were classified')
        return None, mixing_out

    # Select the mixing matrix rows corresponding to the motion ICs
    aggr_mixing_arr = mixing_arr[motion_ics, :].T

    # add one to motion_ic_indices to match melodic report.
    pd.DataFrame(
        aggr_mixing_arr,
        columns=[f'aroma_motion_{x + 1:02d}' for x in motion_ics],
    ).to_csv(aroma_confounds, sep='\t', index=None)

    return aroma_confounds, mixing_out


class _ICADenoiseInputSpec(BaseInterfaceInputSpec):
    method = traits.Enum('aggr', 'nonaggr', 'orthaggr', mandatory=True, desc='denoising method')
    bold_file = File(exists=True, mandatory=True, desc='input file to denoise')
    mask_file = File(exists=True, mandatory=True, desc='mask file')
    mixing = File(exists=True, mandatory=True, desc='mixing matrix file')
    metrics = File(exists=True, mandatory=True, desc='metrics file')
    skip_vols = traits.Int(
        desc=(
            'Number of non steady state volumes identified. '
            'Will be removed from mixing, but not bold'
        ),
    )


class _ICADenoiseOutputSpec(TraitedSpec):
    denoised_file = File(exists=True, desc='denoised output file')


class ICADenoise(SimpleInterface):
    """Denoise data using ICA components."""

    input_spec = _ICADenoiseInputSpec
    output_spec = _ICADenoiseOutputSpec

    def _run_interface(self, runtime):
        import numpy as np
        import pandas as pd
        from nilearn.maskers import NiftiMasker
        from nilearn.masking import apply_mask, unmask

        method = self.inputs.method
        bold_file = self.inputs.bold_file
        mixing_file = self.inputs.mixing
        metrics_file = self.inputs.metrics

        # Load the mixing matrix. It doesn't have a header row
        mixing = np.loadtxt(mixing_file)
        mixing = mixing[self.inputs.skip_vols :, :]

        # Split up component time series into accepted and rejected components
        metrics_df = pd.read_table(metrics_file)
        rejected_idx = metrics_df.loc[metrics_df['classification'] == 'rejected'].index.values
        accepted_idx = metrics_df.loc[metrics_df['classification'] == 'accepted'].index.values
        rejected_components = mixing[:, rejected_idx]
        accepted_components = mixing[:, accepted_idx]
        # Z-score all of the components
        rejected_components = stats.zscore(rejected_components, axis=0)
        accepted_components = stats.zscore(accepted_components, axis=0)

        if method == 'aggr':
            # Denoise the data with the motion components
            masker = NiftiMasker(
                mask_img=self.inputs.mask_file,
                standardize_confounds=False,
                standardize=False,
                smoothing_fwhm=None,
                detrend=False,
                low_pass=None,
                high_pass=None,
                t_r=None,  # This shouldn't be necessary since we aren't bandpass filtering
                reports=False,
            )

            # Denoise the data by fitting and transforming the data file using the masker
            denoised_img_2d = masker.fit_transform(bold_file, confounds=rejected_components)

            # Transform denoised data back into 4D space
            denoised_img = masker.inverse_transform(denoised_img_2d)
        elif method == 'orthaggr':
            # Regress the good components out of the bad time series to get "pure evil" regressors
            betas = np.linalg.lstsq(accepted_components, rejected_components, rcond=None)[0]
            pred_bad_timeseries = np.dot(accepted_components, betas)
            orth_bad_timeseries = rejected_components - pred_bad_timeseries

            # Once you have these "pure evil" components, you can denoise the data
            masker = NiftiMasker(
                mask_img=self.inputs.mask_file,
                standardize_confounds=False,
                standardize=False,
                smoothing_fwhm=None,
                detrend=False,
                low_pass=None,
                high_pass=None,
                t_r=None,  # This shouldn't be necessary since we aren't bandpass filtering
                reports=False,
            )

            # Denoise the data by fitting and transforming the data file using the masker
            denoised_img_2d = masker.fit_transform(bold_file, confounds=orth_bad_timeseries)

            # Transform denoised data back into 4D space
            denoised_img = masker.inverse_transform(denoised_img_2d)
        else:
            # Non-aggressive denoising
            # Apply the mask to the data image to get a 2d array
            data = apply_mask(bold_file, self.inputs.mask_file)

            # Fit GLM to accepted components and rejected components (after adding a constant term)
            regressors = np.hstack(
                (
                    rejected_components,
                    accepted_components,
                    np.ones((mixing.shape[0], 1)),
                ),
            )
            betas = np.linalg.lstsq(regressors, data, rcond=None)[0][:-1]

            # Denoise the data using the betas from just the bad components
            confounds_idx = np.arange(rejected_components.shape[1])
            pred_data = np.dot(rejected_components, betas[confounds_idx, :])
            data_denoised = data - pred_data

            # Save to file
            denoised_img = unmask(data_denoised, self.inputs.mask_file)

        self._results['denoised_file'] = os.path.abspath('denoised.nii.gz')
        denoised_img.to_filename(self._results['denoised_file'])

        return runtime
