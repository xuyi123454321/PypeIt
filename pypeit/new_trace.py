import os
import time
import inspect
import warnings
from collections import Counter, OrderedDict

import numpy as np

from scipy import ndimage, special, signal

import matplotlib
from matplotlib import pyplot as plt
from matplotlib import ticker, rc

from sklearn.decomposition import PCA

from astropy.io import fits
from astropy.stats import sigma_clipped_stats

from pypeit.bitmask import BitMask
from pypeit.core import pydl
from pypeit.core import procimg
from pypeit.core import arc
#from pypeit.core import extract
from pypeit import utils
from pypeit import ginga
from pypeit import masterframe
from pypeit import msgs
from pypeit.traceimage import TraceImage
from pypeit.spectrographs.util import load_spectrograph
from pypeit.par.pypeitpar import TraceSlitsPar
from pypeit import io


def growth_lim(a, lim, fac=1.0, midpoint=None, default=[0., 1.]):
    """
    Calculate bounding limits for an array based on its growth.

    Args:
        a (array-like):
            Array for which to determine limits.
        lim (:obj:`float`):
            Percentage of the array values to cover. Set to 1 if
            provided value is greater than 1.
        fac (:obj:`float`, optional):
            Factor to increase the range based on the growth limits.
            Default is no increase.
        midpoint (:obj:`float`, optional):
            Force the midpoint of the range to be centered on this
            value. Default is the sample median.
        default (:obj:`list`, optional):
            Default limits to return if `a` has no data.

    Returns:
        :obj:`list`: Lower and upper boundaries for the data in `a`.
    """
    # Get the values to plot
    _a = a.compressed() if isinstance(a, np.ma.MaskedArray) else np.asarray(a).ravel()
    if len(_a) == 0:
        # No data so return the default range
        return default

    # Set the starting and ending values based on a fraction of the
    # growth
    _lim = 1.0 if lim > 1.0 else lim
    start, end = (len(_a)*(1.0+_lim*np.array([-1,1]))/2).astype(int)
    if end == len(_a):
        end -= 1

    # Set the full range and multiply it by the provided factor
    srt = np.ma.argsort(_a)
    Da = (_a[srt[end]] - _a[srt[start]])*fac

    # Set the midpoint
    mid = _a[srt[len(_a)//2]] if midpoint is None else midpoint

    # Return the range centered on the midpoint
    return [ mid - Da/2, mid + Da/2 ]


class TraceBitMask(BitMask):
    def __init__(self):
        # TODO: This needs to be an OrderedDict for now to ensure that
        # the bit assigned to each key is always the same. As of python
        # 3.7, normal dict types are guaranteed to preserve insertion
        # order as part of its data model. When/if we require python
        # 3.7, we can remove this (and other) OrderedDict usage in
        # favor of just a normal dict.
        mask = OrderedDict([
                       ('NOEDGE', 'No edge found/input for this trace in this column.'),
                    ('MATHERROR', 'A math error occurred during the calculation (e.g., div by 0)'),
                  ('MOMENTERROR', 'Recentering moment calculation had a large error'),
                   ('LARGESHIFT', 'Recentering resulted in a large shift'),
                ('DISCONTINUOUS', 'Pixel included in a trace but part of a discontinuous segment'),
                    ('DUPLICATE', 'Trace is a duplicate based on trace matching tolerance'),
                     ('TOOSHORT', 'Trace does not meet the minimum length criterion'),
                       ('HITMIN', 'Trace crosses the minimum allowed column'),
                       ('HITMAX', 'Trace crosses the maximum allowed column')
                           ])
        super(TraceBitMask, self).__init__(list(mask.keys()), descr=list(mask.values()))


class EdgeTraceSet(masterframe.MasterFrame):
    r"""
    Object for holding slit edge traces.

    trace_img, mask, and det should be considered mutually exclusive compare to load

    load takes precedence.  I.e., if both trace_img and load are provided, trace_img is ignored!

    if trace_img is provided, the initialization will also run
    :func:`initial_trace` *and* save the output.

    Nominal run:
        - initial_trace
        - moment_refine
        - fit_refine (calls fit_trace)
        - peak_refine (calls peak_trace, which uses both pca_trace and fit_trace)

    Final trace is based on a run of fit_refine that pass through the detected peaks

    TODO: synchronize...

    Args:
        spectrograph (:class:`pypeit.spectrographs.spectrograph.Spectrograph`):
            The `Spectrograph` instance that sets the instrument used to
            take the observations.  Used to set :attr:`spectrograph`.
        par (:class:`pypeit.par.pypeitpar.TraceSlitsPar`):
            The parameters used to guide slit tracing
        master_key (:obj:`str`, optional):
            The string identifier for the instrument configuration.  See
            :class:`pypeit.masterframe.MasterFrame`.
        master_dir (:obj:`str`, optional):
            Path to master frames.
        reuse_masters (:obj:`bool`, optional):
            Load master files from disk, if possible.
        qa_path (:obj:`str`, optional):
            Directory for QA output.
        trace_img (`numpy.ndarray`_, :class:`pypeit.traceimage.TraceImage`, optional):
            2D image used to trace slit edges. If a
            :class:`pypeit.traceimage.TraceImage` is provided, the
            raw files used to construct the image are saved.
        mask (`numpy.ndarray`_, optional):
            Mask for the trace image. Must have the same shape as
            `trace_img`. If None, all pixels are assumed to be valid.
        det (:obj:`int`, optional):
            The 1-indexed detector number that provided the trace
            image. This is *only* used to determine whether or not
            bad columns in the image are actually along columns or
            along rows, as determined by :attr:`spectrograph` and the
            result of a call to
            :func:`pypeit.spectrograph.Spectrograph.raw_is_transposed`.
        load (:obj:`bool`, optional):
            Attempt to load existing output.

    Attributes:
        files (:obj:`list`):
            The list of raw files used to constuct the image used to
            detect and trace slit edges. Only defined if a
            :class:`pypeit.traceimage.TraceImage` object is provided
            for tracing.
        nspec (:obj:`int`):
            Number of spectral pixels (rows) in the trace image
            (`axis=0`).
        nspat (:obj:`int`):
            Number of spatial pixels (columns) in the trace image
            (`axis=1`).
        trace (`numpy.ndarray`_):
            The list of unique trace IDs.
        ntrace (:obj:`int`):
            The number of traces.
        spat_img (`numpy.ndarray`_):
            An integer array with the spatial pixel closest to each
            trace edge, as determined by a moment analysis of the
            Sobel-filtered trace image or by a fit to those data.
            Shape is :math:`(N_{\rm spec},N_{\rm trace})`. This is
            identically::

                self.spat_img = np.round(self.spat_cen
                                         if self.spat_fit is None
                                         else self.spat_fit).astype(int)

        spat_cen (`numpy.ndarray`_):
            A floating-point array with the best-fitting (or input)
            centroid for each trace edge. Shape is :math:`(N_{\rm
            spec},N_{\rm trace})`.
        spat_err (`numpy.ndarray`_):
            Error in the best-fitting (or input) centroid of each
            trace edge. Trace centroids without errors are set to -1.
            Shape is :math:`(N_{\rm spec},N_{\rm trace})`.
        spat_msk (`numpy.ndarray`_):
            An integer array with the mask bits assigned to each
            trace centroid; see :class:`TraceBitMask`. Shape is
            :math:`(N_{\rm spec},N_{\rm trace})`.
        bitmask (:class:`TraceBitMask`):
            Object used to manipulate/query the mask bits.
    """
    master_type = 'Trace'   # For MasterFrame base
    def __init__(self, spectrograph, par, master_key=None, master_dir=None, reuse_masters=False,
                 qa_path=None, trace_img=None, mask=None, det=1, load=False):

        masterframe.MasterFrame.__init__(self, self.master_type, master_dir=master_dir,
                                         master_key=master_key, reuse_masters=reuse_masters)

        self.spectrograph = spectrograph    # Spectrograph used to take the data
        self.par = par                      # Parameters used for slit edge tracing
        self.bitmask = TraceBitMask()       # Object used to define and toggle tracing mask bits

        self.files = None               # Files used to construct the trace image
        self.trace_img = None           # The image used to find the slit edges
        self.trace_msk = None           # Mask for the trace image
        # TODO: Need a separate mask for the sobel image?
        self.det = None                 # Detector used for the trace image
        self.sobel_sig = None           # Sobel filtered image used to detect edges
        self.sobel_sig_left = None      # Sobel filtered image used to trace left edges
        self.sobel_sig_righ = None      # Sobel filtered image used to trace right edges
        self.nspec = None               # The shape of the trace image is (nspec,nspat)
        self.nspat = None
        self.traceid = None             # The ID numbers for each trace
        self.spat_img = None            # (Integer) Pixel nearest the slit edge for each trace
        self.spat_cen = None            # (Floating-point) Spatial coordinate of the slit edges
                                        # for each spectral pixel
        self.spat_err = None            # Error in the slit edge spatial coordinate
        self.spat_msk = None            # Mask for the slit edge position for each spectral pixel
        self.spat_fit = None            # The result of modeling the slit edge positions

        self.qa_path = qa_path          # Directory for QA output

        self.log = None                 # Log of methods applied

        if trace_img is not None and load:
            raise ValueError('Arguments trace_img and load are mutually exclusive.  Choose to '
                             'either trace a new image or load a previous trace.')

        if load:
            # Attempt to load an existing master frame
            self.load()

        if trace_img is not None:
            # Provided a trace image so instantiate the object.
            self.initial_trace(trace_img, mask=mask, det=det)

    @property
    def file_path(self):
        """
        Overwrite MasterFrame default to force the file to be gzipped.

        .. todo:
            - Change the MasterFrame default to a compressed file?
        """
        return '{0}.gz'.format(os.path.join(self.master_dir, self.file_name))

    @property
    def ntrace(self):
        """
        The number of edges (left and right) traced.
        """
        return self.traceid.size

    def initial_trace(self, trace_img, mask=None, det=1, save=True):
        """
        Initialize the object for tracing a new image.

        This effectively reinstantiates the object and must be the
        first method called for tracing an image.

        This method does the following:
            - Lightly boxcar smooth the trace image spectrally.
            - Replace bad pixel columns, if a mask is provided
            - Apply a Sobel filter to the trace image along columns
            to detect slit edges using steep positive gradients (left
            edges) and steep negative gradients (right edges). See
            :func:`detect_slit_edges`.
            - Follow the detected left and right edges along
            spectrally adjacent pixels to identify coherent traces.
            See :func:`identify_traces`.
            - Perform some basic handling of orphaned left or right
            edges. See :func:`handle_orphan_edges`.
            - Initialize the attributes that provide the trace
            position for each spectral pixel based on these results.

        The results of this are, by default, saved to the master
        frame; see `save` argument and :func:`save`.

        Args:
            trace_img (`numpy.ndarray`_, :class:`pypeit.traceimage.TraceImage`):
                2D image used to trace slit edges. If a
                :class:`pypeit.traceimage.TraceImage` is provided,
                the raw files used to construct the image are saved.
            mask (`numpy.ndarray`_, optional):
                Mask for the trace image. Must have the same shape as
                `trace_img`. If None, all pixels are assumed to be
                valid.
            det (:obj:`int`, optional):
                The 1-indexed detector number that provided the trace
                image. This is *only* used to determine whether or
                not bad columns in the image are actually along
                columns or along rows, as determined by
                :attr:`spectrograph` and the result of a call to
                :func:`pypeit.spectrograph.Spectrograph.raw_is_transposed`.
            save (:obj:`bool`, optional):
                Save the result to the master frame.
        """
        # Parse the input based on its type
        if isinstance(trace_img, TraceImage):
            self.files = trace_img.files
            _trace_img = trace_img.stack
            # TODO: does TraceImage have a mask?
            # TODO: instead keep the TraceImage object instead of
            # deconstructing it...
        else:
            _trace_img = trace_img

        # Check the input
        if _trace_img.ndim != 2:
            raise ValueError('Trace image must be 2D.')
        self.trace_img = _trace_img
        self.nspec, self.nspat = self.trace_img.shape
        self.trace_msk = np.zeros((self.nspec, self.nspat), dtype=bool) if mask is None else mask
        if self.trace_msk.shape != self.trace_img.shape:
            raise ValueError('Mask is not the same shape as the trace image.')
        self.det = det

        # Lightly smooth the image before using it to trace edges
        _trace_img = ndimage.uniform_filter(self.trace_img, size=(3, 1), mode='mirror')

        # Replace bad-pixel columns if they exist
        # TODO: Do this before passing the image to this function?
        # Instead of just replacing columns, replace all bad pixels...
        if np.any(self.trace_msk):
            # Do we need to replace bad *rows* instead of bad columns?
            flip = self.spectrograph.raw_is_transposed(det=self.det)
            axis = 1 if flip else 0

            # Replace bad columns that cover more than half the image
            bad_cols = np.sum(self.trace_msk, axis=axis) > (self.trace_msk.shape[axis]//2)
            if flip:
                # Deal with the transposes
                _trace_img = procimg.replace_columns(_trace_img.T, bad_cols, copy=True,
                                                     replace_with='linear').T
            else:
                _trace_img = procimg.replace_columns(_trace_img, bad_cols, copy=True,
                                                     replace_with='linear')

        # Filter the trace image and use the filtered image to detect
        # slit edges
        # TODO: Decide if mask should be passed to this or not,
        # currently not...
        self.sobel_sig, edge_img = detect_slit_edges(_trace_img,
                                                     median_iterations=self.par['medrep'],
                                                     sobel_mode=self.par['sobel_mode'],
                                                     sigdetect=self.par['sigdetect'])

        # Empty out the images prepared for left and right tracing
        # until they're needed.
        self.sobel_sig_left = None
        self.sobel_sig_righ = None

        # Identify traces by following the detected edges in adjacent
        # spectral pixels.
        # TODO: Add spectral_memory and minimum_length to par
        trace_id_img = identify_traces(edge_img, spectral_memory=20, minimum_length=100)

        # Update the traces by handling single orphan edges and/or
        # traces without any left or right edges.
        # TODO: Add this threshold to par
        flux_valid = np.median(_trace_img) > 500
        trace_id_img = handle_orphan_edge(trace_id_img, self.sobel_sig, mask=self.trace_msk,
                                          flux_valid=flux_valid, copy=True)

        # Set the ID image to a MaskedArray to ease some subsequent
        # calculations; pixels without a detected edge are masked.
        trace_id_img = np.ma.MaskedArray(trace_id_img, mask=trace_id_img == 0)

        # Find the set of trace IDs; left traces are negative, right
        # traces are positive
        self.traceid = np.unique(trace_id_img.compressed())

        # Initialize the mask bits for the trace coordinates and
        # initialize them all as having no edge
        self.spat_msk = np.zeros((self.nspec, self.ntrace), dtype=self.bitmask.minimum_dtype())
        self.spat_msk = self.bitmask.turn_on(self.spat_msk, 'NOEDGE')

        # Save the input trace edges and remove the mask for valid edge
        # coordinates
        self.spat_img = np.zeros((self.nspec, self.ntrace), dtype=int)
        for i in range(self.ntrace):
            row, col = np.where(np.invert(trace_id_img.mask)
                                    & (trace_id_img.data == self.traceid[i]))
            self.spat_img[row,i] = col
            self.spat_msk[row,i] = 0            # Turn-off the mask

        # Instantiate objects to store the floating-point trace
        # centroids and errors. Errors are initialized to a nonsensical
        # value to indicate no measurement.
        self.spat_cen = self.spat_img.astype(float)   # This makes a copy
        self.spat_err = np.full((self.nspec, self.ntrace), -1., dtype=float)

        # No fitting has been done yet
        self.spat_fit_type = None
        self.spat_fit = None

        # Restart the log
        self.log = [inspect.stack()[0][3]]

        # Save if requested
        if save:
            self.save()

    def save(self, outfile=None, overwrite=True, checksum=True):
        """
        Save the trace object to a file for full recall.

        Args:
            outfile (:obj:`str`, optional):
                Name for the output file.  Defaults to
                :attr:`file_path`.
            overwrite (:obj:`bool`, optional):
                Overwrite any existing file.
            checksum (:obj:`bool`, optional):
                Passed to `astropy.io.fits.HDUList.writeto` to add
                the DATASUM and CHECKSUM keywords fits header(s).
        """
        _outfile = self.file_path if outfile is None else outfile
        # Check if it exists
        if os.path.exists(_outfile) and not overwrite:
            msgs.warn('Master file exists: {0}'.format(_outfile) + msgs.newline()
                      + 'Set overwrite=True to overwrite it.')
            return
        msgs.info('Saving master frame to {0}'.format(_outfile))

        # Determine if the file should be compressed
        compress = False
        if _outfile.split('.')[-1] == 'gz':
            _outfile = _outfile[:_outfile.rfind('.')] 
            compress = True
    
        # Build the primary header
        #   - Initialize with basic metadata
        prihdr = self.initialize_header()
        #   - Add the qa path
        prihdr['QADIR'] = (self.qa_path, 'PypeIt: QA directory')
        #   - Add metadata specific to this class
        prihdr['SPECT'] = (self.spectrograph.spectrograph, 'PypeIt: Spectrograph Name')
        #   - List the processed raw files, if available
        if self.files is not None:
            nfiles = len(self.files)
            ndig = int(np.log10(nfiles))+1
            for i in range(nfiles):
                prihdr['RAW{0}'.format(str(i+1).zfill(ndig))] \
                            = (self.files[i], 'PypeIt: Processed raw file')
        #   - Add the detector number
        prihdr['DET'] = (self.det, 'PypeIt: Detector')
        #   - Add the tracing parameters
        self.par.to_header(prihdr)
        #   - List the completed methods, if there are any
        if self.log is not None:
            ndig = int(np.log10(len(self.log)))+1
            for i,m in enumerate(self.log):
                prihdr['LOG{0}'.format(str(i+1).zfill(ndig))] \
                        = (m, '{0}: Completed method'.format(self.__class__.__name__))
        #   - Indicate the type if fit (TODO: Keep the fit parameters?)
        fithdr = fits.Header()
        fithdr['FITFUNC'] = 'None' if self.spat_fit_type is None else self.spat_fit_type

        # Only put the definition of the bits in the trace mask in the
        # header of the appropriate extension.
        mskhdr = fits.Header()
        self.bitmask.to_header(mskhdr)

        # Write the fits file; note not everything is written. Some
        # arrays are reconstruced by the load function.
        fits.HDUList([fits.PrimaryHDU(header=prihdr),
                      fits.ImageHDU(data=self.trace_img, name='TRACEIMG'),
                      fits.ImageHDU(data=self.trace_msk.astype(np.int16), name='TRACEMSK'),
                      fits.ImageHDU(data=self.sobel_sig, name='SOBELSIG'),
                      fits.ImageHDU(data=self.traceid, name='TRACEID'),
                      fits.ImageHDU(data=self.spat_cen, name='CENTER'),
                      fits.ImageHDU(data=self.spat_err, name='CENTER_ERR'),
                      fits.ImageHDU(header=mskhdr, data=self.spat_msk, name='CENTER_MASK'),
                      fits.ImageHDU(header=fithdr, data=self.spat_fit, name='CENTER_FIT'),
                    ]).writeto(_outfile, overwrite=True, checksum=checksum)

        # Compress the file if the output filename has a '.gz'
        # extension
        if compress:
            io.compress_file(_outfile, overwrite=overwrite)
            _outfile = '{0}.gz'.format(_outfile)

        msgs.info('Master frame written to {0}'.format(_outfile))

    def load(self):
        """
        Load and reinitialize the trace data.

        Data is read from :attr:`file_path` and used to overwrite any
        internal data. Specific comparisons of the saved data are
        performed to ensure the file is consistent with having been
        written by an identical instantiation; see :func:`_reinit`.

        To load a full :class:`EdgeTraceSet` from a file, instantiate
        using :func:`from_file`.

        Raises:
            FileNotFoundError:
                Raised if no data has been written for this master
                frame.
            ValueError:
                Raised if validation of the data fails (actually
                raised by :func:`_reinit`).
        """
        filename = self.file_path
        # Check the file exists
        if not os.path.isfile(filename):
            raise FileNotFoundError('File does not exit: {0}'.format(filename))
        with fits.open(filename) as hdu:
            # Re-initialize and validate
            self._reinit(hdu)

    @classmethod
    def from_file(cls, filename):
        """
        Instantiate using data from a file.

        To reload data that has been saved for an existing
        instantiation, use :func:`load`.

        Args:
            filename (:obj:`str`):
                Fits file produced by :func:`EdgeTraceSet.save`.
        """
        # Check the file exists
        if not os.path.isfile(filename):
            raise FileNotFoundError('File does not exit: {0}'.format(filename))
        print('Loading EdgeTraceSet data from: {0}'.format(filename))
        with fits.open(filename) as hdu:
            # TODO: Setting reuse_masters seems superfluous here...
            # Instantiate the object
            this = cls(load_spectrograph(hdu[0].header['SPECT']),
                       TraceSlitsPar.from_header(hdu[0].header),
                       master_key=hdu[0].header['MSTRKEY'],
                       master_dir=hdu[0].header['MSTRDIR'],
                       reuse_masters=hdu[0].header['MSTRREU'], qa_path=hdu[0].header['QADIR'])

            # Re-initialize and validate
            # TODO: Apart from the bitmask, validation is also superfluous
            this._reinit(hdu)
        return this

    def _reinit(self, hdu, validate=True):
        """
        Reinitialize the internals based on the provided fits HDU.

        Args:
            hdu (`astropy.io.fits.Header`):
                The fits data used to reinitialize the object written
                by :func:`save`.
            validate (:obj:`bool`, optional):
                Validate that the spectrograph, parameter set, and
                bitmask have not changed between the current internal
                values and the values read from the fits file. The
                method raises an error if the spectrograph or
                parameter set are different. If the bitmask is
                different, a warning is issued and the bitmask
                defined by the header is used instead of the existing
                :attr:`bitmask`.
        """
        # Read and assign data from the fits file
        self.files = io.parse_hdr_key_group(hdu[0].header, prefix='RAW')
        if len(self.files) == 0:
            self.files = None
        self.trace_img = hdu['TRACEIMG'].data
        self.nspec, self.nspat = self.trace_img.shape
        self.trace_msk = hdu['TRACEMSK'].data.astype(bool)
        self.det = hdu[0].header['DET']
        self.sobel_sig = hdu['SOBELSIG'].data
        self.traceid = hdu['TRACEID'].data
        self.spat_cen = hdu['CENTER'].data
        self.spat_err = hdu['CENTER_ERR'].data
        self.spat_msk = hdu['CENTER_MASK'].data
        self.spat_fit = hdu['CENTER_FIT'].data
        self.spat_fit_type = None if hdu['CENTER_FIT'].header['FITFUNC'] == 'None' \
                                else hdu['CENTER_FIT'].header['FITFUNC']

        self.spat_img = np.round(self.spat_cen if self.spat_fit is None
                                 else self.spat_fit).astype(int)

        self.log = io.parse_hdr_key_group(hdu[0].header, prefix='LOG')

        # TODO: Recalculate Sobel left and right images instead of
        # setting them to None?
        self.sobel_sig_left = None
        self.sobel_sig_righ = None

        # Finished, if not validating
        if not validate:
            return

        # Test the bitmask has the same keys and key values
        hdr_bitmask = BitMask.from_header(hdu['CENTER_MASK'].header)
        if hdr_bitmask.bits != self.bitmask.bits:
            warnings.warn('The bitmask in this fits file appear to be out of date!  Will continue '
                          'by using old bitmask but errors may occur.  You should recreate this '
                          'master frame.')
            self.bitmask = hdr_bitmask

        # Test the spectrograph is the same
        if self.spectrograph.spectrograph != hdu[0].header['SPECT']:
            raise ValueError('Data used for this master frame was from a different spectrograph!')

        # Test the parameters used are the same
        par = TraceSlitsPar.from_header(hdu[0].header)
        if self.par.data != par.data:
            # TODO: The above inequality works for non-nested ParSets,
            # but will need to be more careful for nested ones, or just
            # avoid writing nested ParSets to headers...
            raise ValueError('Parameters used to construct this master used different parameters!')

    def qa_plot(self, fileroot=None, min_spat=20):
        """
        Build a series of QA plots showing the edge traces.

        Args:
            fileroot (:obj:`str`, optional):
                Root name for the output files. The number of output
                files depends on the layout and the number of traces
                found. If None, plots are displayed interactively.
            min_spat (:obj:`int`, optional):
                Minimum number of spectral pixels to plot for each
                trace. If None, set to twice the difference between
                the minimum and maximum centroid of the plotted
                trace.

        """

        # Restore matplotlib defaults
        matplotlib.rcParams.update(matplotlib.rcParamsDefault)

        # Set font size
        rc('font', size=8)

        # Spectral pixel coordinate vector and global plot limits
        spec = np.arange(self.nspec)
        xlim = [-1,self.nspec]
        img_zlim = growth_lim(self.trace_img, 0.95, fac=1.05)
        sob_zlim = growth_lim(self.sobel_sig, 0.95, fac=1.05)

        # Set figure
        w,h = plt.figaspect(1)
        fig = plt.figure(figsize=(1.5*w,1.5*h))

        # Grid for plots
        n = np.array([2,3])
        buff = np.array([0.05, 0.03])
        strt = np.array([0.07, 0.04])
        end = np.array([0.99, 0.99])
        delt = (end-(n-1)*buff-strt)/n

        # Determine the number of plot pages
        npages = self.ntrace//int(np.prod(n))
        if npages * np.prod(n) < self.ntrace:
            npages += 1
        ndig = int(np.log10(npages))+1

        # Make plots
        j = 0
        page = 0
        msgs.info('Constructing Trace QA plots')
        for i in range(self.ntrace):

            # Plot index
            jj = j//n[0]
            ii = j - jj*n[0]

            # Plot coordinates
            ax_x = strt[0]+ii*(buff[0]+delt[0])
            ax_y0 = strt[1]+(n[1]-jj-1)*(buff[1]+delt[1])

            # Spatial pixel plot limits for this trace
            indx = self.spat_msk[:,i] == 0
            ylim = growth_lim(self.spat_cen[indx,i], 1.0, fac=2.0)
            if min_spat is not None and np.diff(ylim) < min_spat:
                ylim = np.sum(ylim)/2 + np.array([-1,1])*min_spat/2

            # Plot the trace image and the fit (if it exists)
            ax = fig.add_axes([ax_x, ax_y0 + 2*delt[1]/3, delt[0], delt[1]/3.])
            ax.minorticks_on()
            ax.tick_params(which='major', length=10, direction='in', top=True, right=True)
            ax.tick_params(which='minor', length=5, direction='in', top=True, right=True)
            ax.set_xlim(xlim)
            ax.set_ylim(ylim)
            ax.xaxis.set_major_formatter(ticker.NullFormatter())
            ax.imshow(self.trace_img.T, origin='lower', interpolation='nearest', vmin=img_zlim[0],
                      vmax=img_zlim[1], aspect='auto')
            if self.spat_fit is not None:
                ax.plot(spec, self.spat_fit[:,i], color='C3' if self.traceid[i] < 0 else 'C1')
            ax.text(0.95, 0.8, 'Trace {0}'.format(self.traceid[i]), ha='right', va='center',
                    transform=ax.transAxes, fontsize=12)

            # Plot the filtered image and the fit (if it exists)
            ax = fig.add_axes([ax_x, ax_y0 + delt[1]/3, delt[0], delt[1]/3.])
            ax.minorticks_on()
            ax.tick_params(which='major', length=10, direction='in', top=True, right=True)
            ax.tick_params(which='minor', length=5, direction='in', top=True, right=True)
            ax.set_xlim(xlim)
            ax.set_ylim(ylim)
            ax.xaxis.set_major_formatter(ticker.NullFormatter())
            ax.imshow(self.sobel_sig.T, origin='lower', interpolation='nearest', vmin=sob_zlim[0],
                      vmax=sob_zlim[1], aspect='auto')
            if self.spat_fit is not None:
                ax.plot(spec, self.spat_fit[:,i], color='C3' if self.traceid[i] < 0 else 'C1')
            if ii == 0:
                ax.text(-0.13, 0.5, 'Spatial Coordinate (pix)', ha='center', va='center',
                        transform=ax.transAxes, rotation='vertical')

            # Plot the trace centroids and the fit (if it exists)
            ax = fig.add_axes([ax_x, ax_y0, delt[0], delt[1]/3.])
            ax.minorticks_on()
            ax.tick_params(which='major', length=10, direction='in', top=True, right=True)
            ax.tick_params(which='minor', length=5, direction='in', top=True, right=True)
            ax.set_xlim(xlim)
            ax.set_ylim(ylim)
            ax.scatter(spec[indx], self.spat_cen[indx,i], marker='.', s=50, color='k', lw=0)
            nindx = np.invert(indx)
            if np.any(nindx):
                ax.scatter(spec[nindx], self.spat_cen[nindx,i], marker='x', s=30, color='0.5',
                           lw=0.5)
            if self.spat_fit is not None:
                ax.plot(spec, self.spat_fit[:,i], color='C3' if self.traceid[i] < 0 else 'C1')
            if jj == n[1]-1:
                ax.text(0.5, -0.3, 'Spectral Coordinate (pix)', ha='center', va='center',
                        transform=ax.transAxes)

            # Prepare for the next trace plot
            j += 1
            if j == np.prod(n) or i == self.ntrace-1:
                j = 0
                if fileroot is None:
                    plt.show()
                else:
                    page += 1
                    ofile = os.path.join(self.qa_path,
                                         '{0}_{1}.png'.format(fileroot, str(page).zfill(ndig)))
                    fig.canvas.print_figure(ofile, bbox_inches='tight')
                    print('Finished page {0}/{1}'.format(page, npages))
                fig.clear()
                plt.close(fig)
                fig = plt.figure(figsize=(1.5*w,1.5*h))

    def _side_dependent_sobel(self, side):
        """
        Return the Sobel sigma image relevant to tracing the given
        side.

        The calculation of the side-dependent Sobel image should only
        need to be done once per side per instantiation. Unless they
        are reset to None (such as when the object is
        reinstantiated), multiple calls to this function allow for
        the data to be "lazy loaded" by performing the calculation
        once and then keeping the result in memory.

        Args:
            side (:obj:`str`):
                The side to return; must be 'left' or 'right'
                (case-sensitive).
    
        Returns:
            `numpy.ndarray`_: The manipulated Sobel image relevant to
            tracing the specified edge side.
        """
        # TODO: Add boxcar to TraceSlitsPar
        boxcar = 5
        if side == 'left':
            if self.sobel_sig_left is None:
                self.sobel_sig_left = prepare_sobel_for_trace(self.sobel_sig, boxcar=boxcar,
                                                              side='left')
            return self.sobel_sig_left
        if side == 'right':
            if self.sobel_sig_righ is None:
                self.sobel_sig_righ = prepare_sobel_for_trace(self.sobel_sig, boxcar=boxcar,
                                                              side='right')
            return self.sobel_sig_righ

    def moment_refine(self, width=6.0, maxshift_start=0.5, maxshift_follow=None, maxerror=0.2,
                      continuous=True, match_tolerance=3., minimum_length=None, clip=True):
        """
        Refine the traces using a moment analysis and assess the
        resulting traces.

        For each set of edges (left and right), this method uses
        :func:`follow_trace_moment` to refine the centroids of the
        currently identified traces. The resulting traces are then
        checked that they cover at least a minimum fraction of the
        detector, whether or not they hit the detector edge, and
        whether or not they cross one another; see
        :func:`check_traces`. These two operates are done iteratively
        until all input traces are either refined or flagged for
        deletion.

        Nominally, this method should be run directly after
        :func:`initial_trace`.

        .. warning::
            - This function modifies the internal trace arrays **in
            place**.
            - Because this changes :attr:`spat_cen` and
            :attr:`spat_err`, any model fitting of these data are
            erased by this function! I.e., :attr:`spat_fit` and
            :attr:`spat_fit_type` are set to None.

        Args:
            width (:obj:`float`, `numpy.ndarray`_, optional):
                The size of the window about the provided starting
                center for the moment integration window. See
                :func:`recenter_moment`.
            maxshift_start (:obj:`float`, optional):
                Maximum shift in pixels allowed for the adjustment of
                the first row analyzed, which is the row that has the
                most slit edges that cross through it.
            maxshift_follow (:obj:`float`, optional):
                Maximum shift in pixels between traces in adjacent rows
                as the routine follows the trace away from the first row
                analyzed.  If None, use :attr:`par['maxshift']`.
            maxerror (:obj:`float`, optional):
                Maximum allowed error in the adjusted center of the
                trace returned by :func:`recenter_moment`.
            continuous (:obj:`bool`, optional):
                Keep only the continuous part of the traces from the
                starting row.
            match_tolerance (:obj:`float`, optional):
                If the minimum difference in trace centers among all
                image rows is less than this tolerance, the traces
                are considered to be for the same slit edge and one
                of them is removed.
            minimum_length (:obj:`float`, optional):
                Traces that cover less than this **fraction** of the
                input image are removed. If None, no traces are
                rejected based on length.
            clip (:obj:`bool`, optional):
                Remove traces that are masked as bad and reorder the
                trace IDs.
        """
        # TODO: How many of the other parameters of this function
        # should be added to TraceSlitsPar.
        _maxshift_follow = self.par['maxshift'] if maxshift_follow is None else maxshift_follow

        # To improve performance, generate bogus ivar and mask once
        # here so that they don't have to be generated multiple times.
        # TODO: Keep these as work space as class attributes?
        ivar = np.ones_like(self.sobel_sig, dtype=float)
        _mask = np.zeros_like(self.sobel_sig, dtype=bool) \
                    if self.trace_msk is None else self.trace_msk

        # Book-keeping objects to keep track of which traces have been
        # analyzed and which ones should be removed
        untraced = np.ones(self.ntrace, dtype=bool)
        rmtrace = np.zeros(self.ntrace, dtype=bool)

        # To hold the refined traces and mask
        cen = np.zeros_like(self.spat_cen)
        err = np.zeros_like(self.spat_err)
        msk = np.zeros_like(self.spat_msk)

        # Refine left then right
        for side in ['left', 'right']:
            
            # Get the image relevant to tracing this side
            _sobel_sig = self._side_dependent_sobel(side)

            # Identify the traces on the correct side: Traces on the
            # left side are negative.
            this_side = self.traceid < 0 if side == 'left' else self.traceid > 0

            # Loop continues until all traces are refined
            i = 0
            while np.any(this_side & untraced):
                print('Iteration {0} for {1} side'.format(i+1, side))

                # TODO: Deal with single untraced edge

                # Get the traces to refine
                indx = this_side & untraced
                print('Number to retrace: {0}'.format(np.sum(indx)))

                # Find the most common row index
                start_row = most_common_trace_row(self.spat_msk[:,indx] > 0)
                print('Starting row is: {0}'.format(start_row))

                # Trace starting from this row
                print('Tracing')
                cen[:,indx], err[:,indx], msk[:,indx] \
                        = follow_trace_moment(_sobel_sig, start_row, self.spat_img[start_row,indx],
                                              ivar=ivar, mask=_mask, width=width,
                                              maxshift_start=maxshift_start,
                                              maxshift_follow=_maxshift_follow,
                                              maxerror=maxerror, continuous=continuous,
                                              bitmask=self.bitmask)

                # Check the traces
                mincol = None if side == 'left' else 0
                maxcol = _sobel_sig.shape[1]-1 if side == 'left' else None
                good, bad = self.check_traces(cen, err, msk, subset=indx,
                                              match_tolerance=match_tolerance,
                                              minimum_length=minimum_length,
                                              mincol=mincol, maxcol=maxcol)

                # Save the results and update the book-keeping
                self.spat_cen[:,good] = cen[:,good]
                self.spat_err[:,good] = err[:,good]
                self.spat_msk[:,good | bad] = msk[:,good | bad]
                untraced[good | bad] = False
                rmtrace[bad] = True

                # Increment the iteration counter
                i += 1

        # Update the image coordinates
        self.spat_img = np.round(self.spat_cen).astype(int)

        # Erase any previous fitting results
        self.spat_fit_type = None
        self.spat_fit = None

        # Remove bad traces and re-order the trace IDs
        if clip:
            self.remove_traces(rmtrace, sortid=True)

        # Add to the log
        self.log += [inspect.stack()[0][3]]

    def check_traces(self, cen, err, msk, subset=None, match_tolerance=3., minimum_length=None,
                     mincol=None, maxcol=None):
        r"""
        Validate new trace data to be added.

            - Remove duplicates based on the provided matching
              tolerance.
            - Remove traces that do not cover at least some fraction of
              the detector.
            - Remove traces that are at a minimum or maximum column
              (typically the edge of the detector).

        .. todo::
            - Allow subset to be None and check for repeat of every
            trace with every other trace.

        .. note::
            - `msk` is edited in-place!

        Args:
            cen (`numpy.ndarray`_):
                The adjusted center of the refined traces. Shape is
                :math:`(N_{\rm spec}, N_{\rm refine},)`.
            err (`numpy.ndarray`_):
                The errors in the adjusted center of the refined
                traces. Shape is :math:`(N_{\rm spec}, N_{\rm
                refine},)`.
            msk (`numpy.ndarray`_):
                The mask bits for the adjusted center of the refined
                traces. Shape is :math:`(N_{\rm spec}, N_{\rm
                refine},)`.  This is edited in-place!
            subset (`numpy.ndarray`_, optional):

                Boolean array selecting the traces to compare. Shape
                is :math:`(N_{\rm trace},)`, with :math:`N_{\rm
                refine}` True values. It is expected that all the
                traces selected by a subset must be from the same
                slit side (left or right). If None, all traces are
                checked, no repeat traces can be identified.

            match_tolerance (:obj:`float`, optional):
                If the minimum difference in trace centers among all
                image rows is less than this tolerance, the traces
                are considered to be for the same slit edge and one
                of them is removed.
            minimum_length (:obj:`float`, optional):
                Traces that cover less than this **fraction** of the
                input image are removed. If None, no traces clipped.
            mincol (:obj:`int`, optional):
                Clip traces that hit this minimum column value at the
                center row (`self.nspec//2`). If None, no traces
                clipped.
            maxcol (:obj:`int`, optional):
                Clip traces that hit this maximum column value at the
                center row (`self.nspec//2`). If None, no traces
                clipped.

        Returns:
            Returns two boolean arrays selecting the good and bad
            traces.  Shapes are :math:`(N_{\rm trace},)`.
        """
        # The closest image column
        col = np.round(cen).astype(int)

        indx = np.ones(self.ntrace, dtype=bool) if subset is None else subset

        # Find repeat traces; comparison of traces must include
        # unmasked trace data, be traces of the same edge (left or
        # right), and be within the provided matching tolerance
        repeat = np.zeros_like(indx, dtype=bool)
        if subset is not None:
            s = -1 if np.all(self.traceid[indx] < 0) else 1
            compare = (s*self.traceid > 0) & np.invert(indx)
            if np.any(compare):
                # Use masked arrays to ease exclusion of masked data
                _col = np.ma.MaskedArray(np.round(cen).astype(int), mask=msk > 0)
                spat_img = np.ma.MaskedArray(self.spat_img, mask=self.spat_msk > 0)
                mindiff = np.ma.amin(np.absolute(_col[:,indx,None]-spat_img[:,None,compare]),
                                    axis=(0,2))
                # TODO: This tolerance uses the integer image
                # coordinates, not the floating-point centroid
                # coordinates....
                repeat[indx] =  (mindiff.data < match_tolerance) & np.invert(mindiff.mask)
                if np.any(repeat):
                    msk[:,repeat] = self.bitmask.turn_on(msk[:,repeat], 'DUPLICATE')
                    print('Found {0} repeat traces.'.format(np.sum(repeat)))

        # Find short traces
        short = np.zeros_like(indx, dtype=bool)
        if minimum_length is not None:
            short[indx] = (np.sum(msk[:,indx] == 0, axis=0) < minimum_length*self.nspec)
            if np.any(short):
                msk[:,short] = self.bitmask.turn_on(msk[:,short], 'TOOSHORT')
                print('Found {0} short traces.'.format(np.sum(short)))

        # Find traces that are at the minimum column at the center row
        # TODO: Why only the center row?
        hit_min = np.zeros_like(indx, dtype=bool)
        if mincol is not None:
            hit_min[indx] = (col[self.nspec//2,indx] <= mincol) & (msk[self.nspec//2,indx] == 0)
            if np.any(hit_min):
                msk[:,hit_min] = self.bitmask.turn_on(msk[:,hit_min], 'HITMIN')
                print('{0} traces hit the minimum centroid value.'.format(np.sum(hitmin)))
            
        # Find traces that are at the maximum column at the center row
        # TODO: Why only the center row?
        hit_max = np.zeros_like(indx, dtype=bool)
        if maxcol is not None:
            hit_max[indx] = (col[self.nspec//2,indx] >= maxcol) & (msk[self.nspec//2,indx] == 0)
            if np.any(hit_max):
                msk[:,hit_max] = self.bitmask.turn_on(msk[:,hit_max], 'HITMAX')
                print('{0} traces hit the maximum centroid value.'.format(np.sum(hitmax)))

        # Good traces
        bad = indx & (repeat | short | hit_min | hit_max)
        print('Identified {0} bad traces in all.'.format(np.sum(bad)))
        good = indx & np.invert(bad)
        return good, bad

    def remove_traces(self, indx, sortid=False):
        r"""
        Remove a set of traces.

        Args:
            indx (array-like):
                The boolean array with the traces to remove. Length
                must be :math:`(N_{\rm trace},)`.
            sortid (:obj:`bool`, optional):
                Re-sort the trace IDs to be sequential in the spatial
                direction.  See :func:`resort_trace_ids`
        """
        keep = np.invert(indx)
        self.spat_img = self.spat_img[:,keep]
        self.spat_cen = self.spat_cen[:,keep]
        self.spat_err = self.spat_err[:,keep]
        self.spat_msk = self.spat_msk[:,keep]
        if self.spat_fit is not None:
            self.spat_fit = self.spat_fit[:,keep]
        self.traceid = self.traceid[keep]

        if sortid:
            self.resort_trace_ids()

    def resort_trace_ids(self):
        """
        Re-sort the trace IDS to be sequential in the spatial
        direction.  Attributes are edited in-place.
        """
        # Sort the traces by their spatial position (always use
        # measured positions even if fit positions are available)
        srt = np.argsort(np.mean(self.spat_cen, axis=0))

        self.traceid = self.traceid[srt]
        self.spat_img = self.spat_img[:,srt]
        self.spat_cen = self.spat_cen[:,srt]
        self.spat_err = self.spat_err[:,srt]
        self.spat_msk = self.spat_msk[:,srt]
        if self.spat_fit is not None:
            self.spat_fit = self.spat_fit[:,srt]

        # Reorder the trace numbers
        indx = self.traceid < 0
        self.traceid[indx] = -1-np.arange(np.sum(indx))
        indx = np.invert(indx)
        self.traceid[indx] = 1+np.arange(np.sum(indx))

    def current_trace_img(self):
        """
        Return an image with the trace IDs at the locations of each
        edge in the original image.
        """
        edge_img = np.zeros((self.nspec, self.nspat), dtype=int)
        i = np.tile(np.arange(self.nspec), (self.ntrace,1)).T.ravel()
        edge_img[i, self.spat_img.ravel()] = np.tile(self.traceid, (self.nspec,1)).ravel()
        return edge_img

    def fit_refine(self, function=None, order=None, weighting='uniform', fwhm=3.0,
                   maxdev=5.0, maxiter=25, niter=9, show_fits=False, idx=None, xmin=None,
                   xmax=None):
        """
        Doc string...
        """
        # Generate bogus ivar and mask once here so that they don't
        # have to be generated multiple times.
        # TODO: Keep these as work space as class attributes?
        ivar = np.ones_like(self.sobel_sig, dtype=float)
        mask = np.zeros_like(self.sobel_sig, dtype=bool) \
                    if self.trace_msk is None else self.trace_msk

        # Parameters
        _order = self.par['trace_npoly'] if order is None else order
        _function = self.par['function'] if function is None else function

        trace_fit = np.zeros_like(self.spat_cen, dtype=float)
        trace_cen = np.zeros_like(self.spat_cen, dtype=float)
        trace_err = np.zeros_like(self.spat_cen, dtype=float)
        bad_trace = np.zeros_like(self.spat_cen, dtype=bool)

        # Fit both sides
        for side in ['left', 'right']:
            
            # Get the image relevant to tracing this side
            _sobel_sig = self._side_dependent_sobel(side)
            # Select traces on this side
            this_side = self.traceid < 0 if side == 'left' else self.traceid > 0
            # Perform the fit
            trace_fit[:,this_side], trace_cen[:,this_side], trace_err[:,this_side], \
                bad_trace[:,this_side], _ \
                    = fit_trace(_sobel_sig, self.spat_cen[:,this_side], _order, ivar=ivar,
                                mask=mask, trace_mask=self.spat_msk[:,this_side] > 0,
                                function=_function, niter=niter, show_fits=show_fits)

        # Save the results of the edge measurements ...
        self.spat_cen = trace_cen
        self.spat_err = trace_err
        # ... and the model fits
        self.spat_fit = trace_fit
        self.spat_fit_type = '{0} : order={1}'.format(_function, _order)
        self.spat_img = np.round(self.spat_fit).astype(int)
        # TODO: Flag pixels with a bad_trace (bad moment measurement)?
        # fit_trace (really recenter_moment) replaces any bad
        # measurement with the input center value, so may not want to
        # flag them. Flagging would likely mean that the traces would
        # be ignored in subsequent analysis...

    def peak_refine(self, npca=None, pca_explained_var=99.8, coeff_npoly_pca=3, peak_thresh=None,
                    smash_range=None, trace_thresh=10.0, trace_median_frac=0.01, fwhm_gaussian=3.0,
                    fwhm_uniform=3.0, order=None, lower=2.0, upper=2.0, maxrej=1, debug=False):
        """
        Doc string...

        unlike peak_trace, must set smash_range=[0,1] if you want to
        smash the full image. Here, smash_range = None means use
        :attr:`par['smash_range']`.

        """

        # TODO: Much of this is identical to fit_refine; abstract to a
        # single function that selects the type of refinement to make?
        # Also check traces after fitting or PCA?

        # Generate bogus ivar and mask once here so that they don't
        # have to be generated multiple times.
        # TODO: Keep these as work space as class attributes?
        ivar = np.ones_like(self.sobel_sig, dtype=float)
        mask = np.zeros_like(self.sobel_sig, dtype=bool) \
                    if self.trace_msk is None else self.trace_msk

        # Parameters
        #   - Edge detection
        _peak_thresh = self.par['sigdetect'] if peak_thresh is None else peak_thresh
        _smash_range = self.par['smash_range'] if smash_range is None else smash_range
        _order = self.par['trace_npoly'] if order is None else order

        # TODO: Mask differently if using spat_cen vs. spat_fit?
        trace_inp = self.spat_cen if self.spat_fit is None else self.spat_fit

        # Get the image relevant to tracing
        _sobel_sig = prepare_sobel_for_trace(self.sobel_sig, boxcar=5, side=None)

        # Find and trace both peaks and troughs in the image
        trace_fit, trace_cen, trace_err, bad_trace, nleft \
                = peak_trace(_sobel_sig, trace_inp, ivar=ivar, mask=mask,
                             trace_mask=self.spat_msk > 0, order=_order, npca=npca,
                             pca_explained_var=pca_explained_var, coeff_npoly_pca=coeff_npoly_pca,
                             fwhm_gaussian=fwhm_gaussian, fwhm_uniform=fwhm_uniform,
                             peak_thresh=_peak_thresh, trace_thresh=trace_thresh,
                             trace_median_frac=trace_median_frac, lower=lower, upper=upper,
                             maxrej=maxrej, smash_range=_smash_range, trough=True, debug=debug)

        # Assess the output
        ntrace = trace_fit.shape[1]
        if ntrace < self.ntrace:
            warnings.warn('Found fewer traces using peak finding than originally available.  '
                          'May want to reset peak threshold.')

        # Reset the trace data
        self.spat_msk = np.zeros_like(bad_trace, dtype=self.bitmask.minimum_dtype())
        if np.any(bad_trace):
            self.spat_msk[bad_trace] = self.bitmask.turn_on(self.spat_msk[bad_trace], 'MATHERROR')
        self.traceid = np.zeros(ntrace, dtype=int)
        self.traceid[:nleft] = -1-np.arange(nleft)
        self.traceid[nleft:] = 1+np.arange(ntrace-nleft)
        self.spat_fit = trace_fit
        self.spat_fit_type = '{0} : order={1}'.format('legendre', _order)
        self.spat_cen = trace_cen
        self.spat_err = trace_err
        self.spat_img = np.round(self.spat_fit).astype(int)
        self.resort_trace_ids()


def detect_slit_edges(flux, mask=None, median_iterations=0, min_sqm=30.,
                      sobel_mode='nearest', sigdetect=30.):
    """
    Find slit edges using the input image.

    The primary algorithm is to run a Sobel filter on the image and
    then trigger on all significant gradients. Positive gradients are
    left edges, negative gradients are right edges.

    Args:
        flux (`numpy.ndarray`_):
            Calibration frame used to identify slit edges.  Likely a
            flat-field image that has been lightly smoothed in the
            spectral direction.  The image should also have its bad
            pixels replaced (see
            :func:`pypeit.core.procimg.replace_columns`).  Its
            orientation *must* have spectra dispersed along rows.
        mask (`numpy.ndarray`_, optional):
            A boolean or integer bad-pixel mask.  If None, all pixels
            are assumed valid.  This is used to ignore features in the
            image that may be due to bad pixels.
        median_iterations (:obj:`int`, optional):
            Number of median smoothing iteration to perform on the trace
            image.  The size of the smoothing is always (7,3).  For
            long-slit data, we recommend `median_iterations=0`.
        min_sqm (:obj:`float`, optional):
            Minimum error used when detecting a slit edge.  TODO: This
            needs a better description.
        sobel_mode (:obj:`str`, optional):
            Mode to use with the Sobel filter.  See
            `scipy.ndimage.sobel`_.
        sigdetect (:obj:`float`, optional):
            Threshold for edge detection.

    Returns:
        Returns two `numpy.ndarray`_ objects: (1) The image of the
        significance of the edge detection in sigma and (2) the array
        isolating the slit edges. In the latter, left edges have a
        value of -1 and right edges have a value of 1.
    """
    # Checks
    if flux.ndim != 2:
        msgs.error('Trace image must be 2D.')
    _mask = np.zeros_like(flux, dtype=int) if mask is None else mask.astype(int)
    if _mask.shape != flux.shape:
        msgs.error('Mismatch in mask and trace image shapes.')

    # Specify how many times to repeat the median filter.  Even better
    # would be to fit the filt/sqrt(abs(binarr)) array with a Gaussian
    # near the maximum in each column
    msgs.info("Detecting slit edges in the trace image")

    # Generate sqrt image
    sqmstrace = np.sqrt(np.abs(flux))

    # Median filter
    # TODO: Add size to parameter list
    for ii in range(median_iterations):
        sqmstrace = ndimage.median_filter(sqmstrace, size=(7, 3))

    # Make sure there are no spuriously low pixels
    sqmstrace[(sqmstrace < 1.0) & (sqmstrace >= 0.0)] = 1.0
    sqmstrace[(sqmstrace > -1.0) & (sqmstrace <= 0.0)] = -1.0

    # Filter with a Sobel
    filt = ndimage.sobel(sqmstrace, axis=1, mode=sobel_mode)
    # Apply the bad-pixel mask
    filt *= (1.0 - _mask)
    # Significance of the edge detection
    sobel_sig = np.sign(filt)*np.power(filt,2)/np.maximum(sqmstrace, min_sqm)

    # First edges assigned according to S/N
    # TODO: why not match the sign of the Sobel image to the edge it
    # traces? I.e., why is the sign flipped?
    tedges = np.zeros(flux.shape, dtype=np.float)
    tedges[np.where(sobel_sig > sigdetect)] = -1.0  # A positive gradient is a left edge
    tedges[np.where(sobel_sig < -sigdetect)] = 1.0  # A negative gradient is a right edge
    
    # Clean the edges
    wcl = np.where((ndimage.maximum_filter1d(sobel_sig, 10, axis=1) == sobel_sig) & (tedges == -1))
    wcr = np.where((ndimage.minimum_filter1d(sobel_sig, 10, axis=1) == sobel_sig) & (tedges == 1))
    edge_img = np.zeros(sobel_sig.shape, dtype=np.int)
    edge_img[wcl] = -1
    edge_img[wcr] = 1

    if mask is not None:
        msgs.info("Applying bad pixel mask")
        edge_img *= (1-_mask)
        sobel_sig *= (1-_mask)

    return sobel_sig, edge_img


def identify_traces(edge_img, max_spatial_separation=4, spectral_memory=10, minimum_length=50):
    """
    Follow slit edges to identify unique slit traces.

    Args:
        edge_img (`numpy.ndarray`_):
            An array marked with -1 for left slit edges and +1 for right
            slit edges and 0 everywhere else.  The image *must* be
            oriented with the spatial dimension primarily along the
            first axis and spectral dimension primarily along the
            second.  See :func:`detect_slit_edges`.
        max_spatial_separation (:obj:`int`, optional):
            The maximum spatial separation between two edges in proximal
            spectral rows before they become separated into different
            slit traces.
        spectral_memory (:obj:`int`, optional):
            The number of previous spectral rows to consider when
            following slits forward.
        minimum_length (:obj:`int`, optional):
            The minimum number of spectral rows in an edge trace.
            Traces that do not meet this criterion are ignored.

    Returns:
        `numpy.ndarray`_: An integer array with trace ID numbers at
        pixels locating the edge of that trace. Negative traces are
        for left edges, positive for right edges. The number of left
        and right traces can be determined using
        :func:`count_edge_traces`. Pixels not associated to any edge
        have a value of 0.
    """
    msgs.info('Finding unique traces among detected edges.')
#    # Check the input
#    if edge_img.ndim > 2:
#        raise ValueError('Provided edge image must be 2D.')
#    if not np.array_equal(np.unique(edge_img), [-1,0,1]):
#        raise ValueError('Edge image must only have -1, 0, or 1 values.')

    # Find the left and right coordinates
    lx, ly = np.where(edge_img == -1)
    rx, ry = np.where(edge_img == 1)
    x = np.concatenate((lx, rx))
    # Put left traces at negative y
    y = np.concatenate((-ly, ry))

    # The trace ID to associate with each coordinate
    trace = np.full_like(x, -1)

    # Loop over spectral channels
    last = 0
    for row in range(np.amin(x), np.amax(x)+1):
        # Find the slit edges in this row
        indx = x == row
        in_row = np.sum(indx)
        if in_row == 0:
            # No slits found in this row
            continue

        # Find the unique edge y positions in the selected set of
        # previous rows and their trace IDs
        prev_indx = np.logical_and(x < row, x > row - spectral_memory)
        if not np.any(prev_indx):
            # This is likely the first row or the first row with any
            # slit edges
            trace[indx] = np.arange(in_row)+last
            last += in_row
            continue
        uniq_y, uniq_i = np.unique(y[prev_indx], return_index=True)
        uniq_t = trace[prev_indx][uniq_i]

        # Assign trace IDs to this row
        #   - First match to any previous IDs
        row_trace = np.full(in_row, -1)
        for i, _y in enumerate(y[indx]):
            dist = np.absolute(uniq_y-_y)
            mindist = np.argmin(dist)
            if dist[mindist] < max_spatial_separation:
                row_trace[i] = uniq_t[mindist]
        #   - Assign new trace IDs to unmatched edges
        unassigned = row_trace == -1
        n_unassigned = np.sum(unassigned)
        row_trace[unassigned] = np.arange(n_unassigned)+last
        last += n_unassigned
        #   - Assign all edges and continue
        trace[indx] = row_trace

    # Reorder the traces and remove any that do not meet the specified
    # length.
    #   - Left edges.  Given negative IDs starting with -1
    indx = y < 0
    left, reconstruct, counts = np.unique(trace[indx], return_inverse=True,
                                             return_counts=True)
#    if np.any(counts > edge_img.shape[0]):
#        warnings.warn('Some traces have more pixels than allowed by the image.  The maximum '
#                      'spatial separation for the edges in a given trace may be too large.')
    good_trace = counts > minimum_length
    left[:] = 0
    left[good_trace] = -1-np.arange(np.sum(good_trace))
    trace[indx] = left[reconstruct]
    #   - Right edges.  Given positive IDs starting with 1
    indx = np.invert(indx)
    right, reconstruct, counts = np.unique(trace[indx], return_inverse=True,
                                              return_counts=True)
#    if np.any(counts > edge_img.shape[0]):
#        warnings.warn('Some traces have more pixels than allowed by the image.  The maximum '
#                      'spatial separation for the edges in a given trace may be too large.')
    good_trace = counts > minimum_length
    right[:] = 0
    right[good_trace] = 1+np.arange(np.sum(good_trace))
    trace[indx] = right[reconstruct]

    # Construct the image with the trace IDs and return
    trace_id_img = np.zeros_like(edge_img, dtype=int)
    trace_id_img[x,np.absolute(y)] = trace
    return trace_id_img


def count_edge_traces(trace_img):
    """
    Count the number of left and right edges traced.

    Args:
        trace_img (`numpy.ndarray`_):
            Image with edge trace pixels numbered by their associated
            trace.  Pixels with positive numbers follow right slit edges
            and negative numbers follow left slit edges.
    
    Returns:
        Two integers with the number of left and right edges,
        respectively.
    """
    # Avoid returning -0
    nleft = np.amin(trace_img)
    return 0 if nleft == 0 else -nleft, np.amax(trace_img)


# TODO: This needs to be better tested
def atleast_one_edge(trace_img, mask=None, flux_valid=True, copy=False):
    """
    Ensure that there is at least one left and one right slit edge
    identified.

    This is especially useful for long slits that fill the full
    detector, e.g. Shane Kast.

    Args:
        trace_img (`numpy.ndarray`_):
            Image with edge trace pixels numbered by their associated
            trace.  Pixels with positive numbers follow right slit edges
            and negative numbers follow left slit edges.
        mask (`numpy.ndarray`_, optional):
            Integer (0 unmasked; 1 masked) or boolean array indicating
            bad pixels in the image.  If None, all pixels are considered
            good.
        flux_valid (:obj:`bool`, optional):
            The flux in the image used to construct the edge traces is
            valid meaning that any problems should not be an issue with
            the trace image itself.
        copy (:obj:`bool`, optional):
            Copy `trace_img` to a new array before making any
            modifications.  Otherwise, `trace_img` is modified in-place.
   
    Returns:
        `numpy.ndarray`_: The modified trace image, which is either a
        new array or points to the in-place modification of `trace_img`
        according to the value of `copy`.  If no slit edges were found
        and the flux in the trace image is invalid (`flux_valid=False`),
        function returns `None`.
    """
    # Get the number of traces
    nleft, nright = count_edge_traces(trace_img)

    # Determine whether or not to edit the image in place
    _trace_img = trace_img.copy() if copy else trace_img

    if nleft != 0 and nright != 0:
        # Don't need to add anything
        return _trace_img

    if nleft == 0 and nright == 0 and not flux_valid:
        # No traces and fluxes are invalid.  Warn the user and continue.
        msgs.warn('Unable to trace any edges!  Image flux is low; check trace image is correct.')
        return None

    # Use the mask to determine the first and last valid pixel column
    sum_bpm = np.ones(trace_img.shape[1]) if mask is None else np.sum(mask, axis=0) 

    if nleft == 0:
        # Add a left edge trace at the first valid column
        msgs.warn('No left edge found. Adding one at the detector edge.')
        gdi0 = np.min(np.where(sum_bpm == 0)[0])
        _trace_img[:,gdi0] = -1

    if nright == 0:
        # Add a right edge trace at the last valid column
        msgs.warn('No right edge found. Adding one at the detector edge.')
        gdi1 = np.max(np.where(sum_bpm == 0)[0])
        _trace_img[:,gdi1] = 1

    return _trace_img


# TODO: This needs to be better tested
def handle_orphan_edge(trace_id_img, sobel_sig, mask=None, flux_valid=True, copy=False):
    """
    In the case of single left/right traces and multiple matching
    traces, pick the most significant matching trace and remove the
    others.

    If *no* left and/or right edge is present, this will add one using
    :func:`atleast_one_edge`.

    Args:
        trace_id_img (`numpy.ndarray`_):
            Image with edge trace pixels numbered by their associated
            trace.  Pixels with positive numbers follow right slit edges
            and negative numbers follow left slit edges.
        sobel_sig (`numpy.ndarray`_):
            Image with the significance of the edge detection.  See
            :func:`detect_slit_edges`.
        mask (`numpy.ndarray`_, optional):
            Integer (0 unmasked; 1 masked) or boolean array indicating
            bad pixels in the image.  If None, all pixels are considered
            good.
        flux_valid (:obj:`bool`, optional):
            The flux in the image used to construct the edge traces is
            valid meaning that any problems should not be an issue with
            the trace image itself.
        copy (:obj:`bool`, optional):
            Copy `trace_id_img` to a new array before making any
            modifications. Otherwise, `trace_id_img` is modified
            in-place.

    Returns:
        `numpy.ndarray`_: The modified trace image, which is either a
        new array or points to the in-place modification of
        `trace_id_img` according to the value of `copy`.
    """
    # Get the number of traces
    nleft, nright = count_edge_traces(trace_id_img)

    if nleft == 0 or nright == 0:
        # Deal with no left or right edges
        _trace_id_img = atleast_one_edge(trace_id_img, mask=mask, flux_valid=flux_valid, copy=copy)
    else:
        # Just do basic setup
        _trace_id_img = trace_id_img.copy() if copy else trace_id_img

    if nleft != 1 and nright != 1 or nleft == 1 and nright == 1:
        # Nothing to do
        return _trace_id_img
    
    if nright > 1:
        # To get here, nleft must be 1.  This is mainly in here for
        # LRISb, which is a real pain..
        msgs.warn('Only one left edge, and multiple right edges.')
        msgs.info('Restricting right edge detection to the most significantly detected edge.')
        # Find the most significant right trace
        best_trace = np.argmin([-np.median(sobel_sig[_trace_id_img==t]) for t in range(nright)])+1
        # Remove the other right traces
        indx = _trace_id_img == best_trace
        _trace_id_img[(_trace_id_img > 0) & np.invert(indx)] = 0
        # Reset the number to a single right trace
        _trace_id_img[indx] = 1
        return _trace_id_img

    # To get here, nright must be 1.
    msgs.warn('Only one right edge, and multiple left edges.')
    msgs.info('Restricting left edge detection to the most significantly detected edge.')
    # Find the most significant left trace
    best_trace = np.argmax([np.median(sobel_sig[_trace_id_img == -t]) for t in range(nleft)])+1
    # Remove the other left traces
    indx = _trace_id_img == best_trace
    _trace_id_img[(_trace_id_img > 0) & np.invert(indx)] = 0
    # Reset the number to a single left trace
    _trace_id_img[indx] = 1

    return _trace_id_img

def most_common_trace_row(trace_mask):
    """
    Find the spectral position (row) that crosses the most traces.

    If provided the mask for a single trace, this just returns the
    median of the unmasked rows.

    Args:
        trace_mask (`numpy.ndarray`_):
            Mask for the trace data (True is bad; False is good). Can
            be a 1D array for a single trace or a 2D array with shape
            (nspec, ntrace) for multiple traces.

    Returns:
        :obj:`int`: The row that crosses the most valid trace data.
    """
    if trace_mask.ndim == 1:
        rows = np.where(np.invert(trace_mask))[0]
        return rows[rows.size//2]
    return Counter(np.where(np.invert(trace_mask))[0]).most_common(1)[0][0]


# TODO: Move this to utils
def boxcar_smooth_rows(img, nave, wgt=None, mode='nearest'):
    """
    Boxcar smooth an image along rows.

    Constructs a boxcar kernel and uses `scipy.ndimage.convolve` to
    smooth the image.  Cannot accommodate masking.

    Args:
        img (`numpy.ndarray`_):
            Image to convolve.
        nave (:obj:`int`):
            Number of pixels along rows for smoothing.
        wgt (`numpy.ndarray`_, optional):
            Image providing weights for each pixel in `img`.  Uniform
            weights are used if none are provided.
        mode (:obj:`str`, optional):
            See `scipy.ndimage.convolve`_.

    Returns:
        `numpy.ndarray`_: The smoothed image
    """
    if wgt is not None and img.shape != wgt.shape:
        raise ValueError('Input image to smooth and weights must have the same shape.')
    if nave > img.shape[0]:
        warnings.warn('Smoothing box is larger than the image size!')

    # Construct the kernel for mean calculation
    _nave = np.fmin(nave, img.shape[0])
    kernel = np.ones((_nave, 1))/float(_nave)

    if wgt is None:
        # No weights so just smooth
        return ndimage.convolve(img, kernel, mode='nearest')

    # Weighted smoothing
    cimg = ndimage.convolve(img*wgt, kernel, mode='nearest')
    wimg = ndimage.convolve(wgt, kernel, mode='nearest')
    smoothed_img = np.ma.divide(cimg, wimg)
    smoothed_img[smoothed_img.mask] = img[smoothed_img.mask]
    return smoothed_img.data


def recenter_moment(flux, xcen, ivar=None, mask=None, ycen=None, weighting='uniform', width=3.0,
                    fill_error=-1.):
    r"""
    Determine weighted centroids of features in a flux image using a
    first-moment calculation.

    Although the input image must be 2D, the calculation is 1D along
    its second axis (axis=1). Specifically, the provided centers
    (:math:`\mu`) and center errors (:math:`\epsilon_\mu`) are

    .. math::

        \mu &= \frac{\sum_i x_i w_i f_i }{\sum_i w_i f_i} \\
        \epsilon_\mu^2 &= \left(\sum_i w_i f_i\right)^{-2}\
                          \sum_i \left[ w_i \epsilon_{f,i}
                                      (x_i - \mu)]^2

    where :math:`x` is the pixel position along the 2nd axis,
    :math:`f` is the flux in that pixel, and :math:`\epsilon_f` is
    the flux error. The weights applied are determined by the
    `weighting` keyword argument (see below).

    The method uses numpy masked arrays to keep track of errors in
    the calculation, likely due to math errors such as divisions by
    0. The returned boolean array indicates when these errors
    occurred, and the method replaces these errors with the original
    centers.

    This is a consolidation of the functionality in trace_fweight.pro
    and trace_gweight.pro from IDLUTILS.

    Revision history:
        - 24-Mar-1999  Written by David Schlegel, Princeton.
        - 27-Jun-2018  Ported to python by X. Prochaska and J. Hennawi

    Args:
        flux (`numpy.ndarray`_):
            Intensity image with shape (ny, nx).
        xcen (`numpy.ndarray`_):
            Initial guesses for centroid. This can either be an 2-d
            array with shape (ny, nc) array, or a 1-d array with
            shape (nc), where nc is the number of coordinates along
            the 2nd axis to recenter.
        ivar (`numpy.ndarray`_, optional):
            Inverse variance of the image intensity.  If not provided,
            unity variance is used.  If provided, must have the same
            shape as `flux`.
        mask (`numpy.ndarray`_, optional):
            Mask for the input image.  True values are ignored, False
            values are included.  If not provided, all pixels are
            included.  If provided, must have the same shape as
            `flux`.
        ycen (:obj:`int`, `numpy.ndarray`_, optional):
            Integer or integer array with the position along the 0th
            axis for the recentering. If None, assume `np.arange(ny)`
            for each coordinate (nc) in `xcen`. If a single number,
            assume the value is identical for all `xcen`. Otherwise,
            must have the same shape as `xcen`.
        weighting (:obj:`str`, optional):
            The weighting to apply to the position within each
            integration window (see `width` below). This must be
            (case-sensitive) either 'uniform' for uniform weighting
            or 'gaussian' for weighting by a Gaussian centered at the
            input guess coordinates and integrated over the pixel
            width.
        width (:obj:`float`, `numpy.ndarray`_, optional):
            This can be a scalar for a fixed window size or an array
            for a coordinate-dependent integration window. If an
            array is provided, it must have the same shape as
            `xcen`. The meaning of the parameter is dependent on the
            value of `weighting` (see above)::

                - `weighting=='uniform'`: The width of the
                integration window (along 2nd axis) centered at the
                input guess coordinate.
                - `weighting=='gaussian'`: The sigma of the Gaussian
                to use for the weighting. The width of the
                integration window (along 2nd axis) centered at the
                input guess coordinate is always 6*width (half-width
                is :math:`3\sigma`).

        fill_error (:obj:`float`, optional):
            Value to use as filler for undetermined centers, resulting
            from either the input mask or computational issues (division
            by zero, etc.; see return description below).

    Returns:
        Three `numpy.ndarray`_ objects are returned, all with the same
        shape as the input coordinates (`xcen`)::
            - The centroid of the flux distribution along the 2nd
            axis of the input image. Masked values (indicated by the
            third object returned) are identical to the input values.
            - The formal propagated error (see equation above) on the
            centroids. Errors are only meaningful if `ivar` is
            provided. Masked values (indicated by the third object
            returned) are set to `fill_error`.
            - A boolean mask for output data; True values should be
            ignored, False values are valid measurements.

    Raises:
        ValueError:
            Raised if input shapes are not correct.
    """

    # TODO: There's a lot of setup overhead to make this a general
    # function. If multiple calls to the function are expected, should
    # gain by only doing this setup once for a given image...
    
    if weighting not in ['uniform', 'gaussian']:
        # TODO: Make it case insensitive?
        raise ValueError('Weighting must be uniform or gaussian')

    # Check the input images
    _ivar = np.ones_like(flux) if ivar is None else ivar
    if _ivar.shape != flux.shape:
        raise ValueError('Inverse variance must have the same shape as the input image.')
    _mask = np.zeros_like(flux, dtype=bool) if mask is None else mask
    if _mask.shape != flux.shape:
        raise ValueError('Pixel mask must have the same shape as the input image.')

    # Process coordinate input
    ny, nx = flux.shape
    _xcen = np.atleast_1d(xcen)       # If entered as a numpy array, the array is not a copy
    ndim = _xcen.ndim
    if ndim > 2:
        raise ValueError('X center array must be 1 or 2 dimensional')
    dim = _xcen.shape
    npix = dim[0]
    if dim == 2 and npix > ny:
        # TODO: Is this test necessary?
        raise ValueError('More locations to trace ({0}) than pixels in the image ({1})!'.format(
                            npix,ny))
    ntrace = 1 if ndim == 1 else dim[1]

    _width = np.atleast_1d(width)     # If entered as a numpy array, the array is not a copy
    if _width.size > 1 and _width.shape == _xcen.shape:
        raise ValueError('Radius must a be either an integer, a floating point number, or an '
                         'ndarray with the same shape and size as xcen.')

    # Copy and normalize the shape of the input (flatten creates a
    # copy); the width needs to be an array for the compressed
    # calculation below.
    _xcen = _xcen.flatten()
    _width = _width.flatten() if _width.size > 1 else np.full(_xcen.size, _width[0], dtype=float)

    # The "radius" of the pixels to cover is either half of the
    # provided width for uniform weighting or 3*width for Gaussian
    # weighting, where width is the sigma of the Gaussian
    _radius = _width/2 if weighting == 'uniform' else _width*3

    # Set the row locations for the recentering
    # TODO: Check that this does not ovewrite ycen
    if ycen is None:
        _ycen = np.arange(npix, dtype=int) if ndim == 1 else np.tile(np.arange(npix), (ntrace,1)).T
    elif not hasattr(ycen, '__len__'):
        # This is specifically set to be the same size as the input
        # xcen (not _xcen)
        _ycen = np.full_like(xcen, ycen, dtype=int)
    else:
        _ycen = np.asarray(ycen)
    if np.amin(_ycen) < 0 or np.amax(_ycen) > ny-1:
        raise ValueError('Input ycen values will run off the image')
    _ycen = _ycen.astype(int).flatten()

    # Check the sizes match
    if _xcen.size != _ycen.size:
        raise ValueError('Number of elements in xcen and ycen must be equal')

    # Window for the integration for each coordinate
    ix1 = np.floor(_xcen - _radius + 0.5).astype(int)
    ix2 = np.floor(_xcen + _radius + 0.5).astype(int)
    fullpix = int(np.amax(np.amin(ix2-ix1)-1,0))
    if weighting == 'uniform':
        fullpix += 3        # To match trace_fweight
    x = ix1[:,None]-1+np.arange(fullpix)[None,:]
    ih = np.clip(x,0,nx-1)

    # Set the weight over the window; masked pixels have 0 weight
    good = ((x >= 0) & (x < nx) & np.invert(_mask[_ycen[:,None],ih]) \
                 & (_ivar[_ycen[:,None],ih] > 0)).astype(int)
    if weighting == 'uniform':
        # Weight according to the fraction of each pixel within in the
        # integration window
        wt = good * np.clip(_radius[:,None] - np.abs(x - _xcen[:,None]) + 0.5,0,1)
    else:
        # Weight according to the integral of a Gaussian over the pixel
        coo = x - _xcen[:,None]
        wt = good * (special.erf((coo+0.5)/np.sqrt(2)/_width[:,None])
                        - special.erf((coo-0.5)/np.sqrt(2)/_width[:,None]))/2.

    # Weight the image data
    fwt = flux[_ycen[:,None],ih] * wt
    # Get the weighted center ...
    sumw = np.sum(fwt, axis=1)
    mu = np.ma.divide(np.sum(fwt*x, axis=1), sumw)
    # ... and the formal error
    mue = np.ma.divide(np.ma.sqrt(np.ma.sum(
                       np.ma.divide(np.square(wt*(x-mu[:,None])), _ivar[_ycen[:,None],ih]),
                       axis=1)), np.absolute(sumw))

    # Replace any bad calculations with the input value
    # TODO: Include error mask (mue.mask as done below) or ignore
    # problems in the error calculation?
    bad = mu.mask | mue.mask
    mu[bad] = _xcen[bad]
    mue[bad] = fill_error

    # Reshape to the correct size for output if more than one trace was
    # input
    if ndim > 1:
        mu = mu.reshape(npix, ntrace)
        mue = mue.reshape(npix, ntrace)
        bad = bad.reshape(npix, ntrace)

    # Make sure to return unmasked arrays
    return mu.data, mue.data, bad


def prepare_sobel_for_trace(sobel_sig, boxcar=5, side='left'):
    """
    Prepare the Sobel filtered image for tracing.

    The method:
        - Flips the value of the Sobel image for the right traces;
        the pixels along right traces are negative.
        - Smooths along rows (spatially)

    Args:           
        sobel_sig (`numpy.ndarray`_):
            Image with the significance of the edge detection; see
            :func:`detect_slit_edges`.
        boxcar (:obj:`int`, optional):
            Boxcar smooth the detection image along rows before
            recentering the edge centers; see
            :func:`boxcar_smooth_rows`. If `boxcar` is less than 1,
            no smoothing is performed.
        side (:obj:`str`, optional):
            The side that the image will be used to trace. In the
            Sobel image, positive values are for left traces,
            negative for right traces. If 'left', the image is
            clipped at a minimum value of -0.1. If 'right', the image
            sign is flipped and then clipped at a minimum of -0.1. If
            None, the image is not flipped or clipped, only smoothed.

    Returns:
        `numpy.ndarray`_: The smoothed image.
    """
    if side not in ['left', 'right', None]:
        raise ValueError('Side must be left, right, or None.')
    if side is None:
        img = sobel_sig
    else:
        img = np.maximum(sobel_sig, -0.1) if side == 'left' \
                    else np.maximum(-1*sobel_sig, -0.1)
    return boxcar_smooth_rows(img, boxcar) if boxcar > 1 else img


def follow_trace_moment(flux, start_row, start_cen, ivar=None, mask=None, width=6.0,
                        maxshift_start=0.5, maxshift_follow=0.15, maxerror=0.2, continuous=True,
                        bitmask=None):
    """
    Follow a set of traces using a moment analysis of the provided
    image.

    Starting from a specified row and input centers along each
    column, attempt to follow a set of traces to both lower and
    higher rows in the provided image.

    Importantly, this function does not treat each row independently
    (as would be the case for a direct call to
    :func:`recenter_moment` for trace positions along all rows), but
    treats the calculation of the centroids sequentially where the
    result for each row is dependent and starts from the result from
    the previous row. The only independent measurement is the one
    performed at the input `start_row`. This function is much slower
    than :func:`recenter_moment` because of this introduced
    dependency.

    Args:
        flux (`numpy.ndarray`_):
            Image used to weight the column coordinates when
            recentering. This should typically be the Sobel filtered
            trace image after adjusting for the correct side and
            performing any smoothing; see
            :func:`prepare_sobel_for_trace`.
        start_row (:obj:`int`):
            Row at which to start the recentering. The function
            begins with this row and then traces first to higher
            indices and then to lower indices. This will almost
            certainly need to be different than the default, which is
            to start at the first row.
        start_cen (:obj:`int`, `numpy.ndarray`_, optional):
            One or more trace coordinates to recenter. If an array,
            must be 1D.
        ivar (`numpy.ndarray`_, optional):
            Inverse variance in the weight image. If not provided,
            unity variance is assumed. Used for the calculation of
            the errors in the moment analysis. If this is not
            provided, be careful with the value set for
            `maxerror_center` (see below).
        mask (`numpy.ndarray`_, optional):
            A boolean mask used to ignore pixels in the weight image.
            Pixels to ignore are masked (`mask==True`), pixels to
            analyze are not masked (`mask==False`)
        width (:obj:`float`, `numpy.ndarray`_, optional):
            The size of the window about the provided starting center
            for the moment integration window. See
            :func:`recenter_moment`.
        maxshift_start (:obj:`float`, optional):
            Maximum shift in pixels allowed for the adjustment of the
            first row analyzed, which is the row that has the most
            slit edges that cross through it.
        maxshift_follow (:obj:`float`, optional):
            Maximum shift in pixels between traces in adjacent rows
            as the routine follows the trace away from the first row
            analyzed.
        maxerror (:obj:`float`, optional):
            Maximum allowed error in the adjusted center of the trace
            returned by :func:`recenter_moment`.
        continuous (:obj:`bool`, optional):
            Keep only the continuous part of the traces from the
            starting row.
        bitmask (:class:`pypeit.bitmask.BitMask`, optional):
            Object used to flag traces. If None, assessments use
            boolean to flag traces. If not None, errors will be
            raised if the object cannot interpret the correct flag
            names defined. In addition to flags used by
            :func:`_recenter_trace_row`, this function uses the
            DISCONTINUOUS flag.

    Returns:
        Two numpy arrays are returned, the optimized center and an
        estimate of the error; the arrays are masked arrays if
        `start_cen` is provided as a masked array.
    """
    if flux.ndim != 2:
        raise ValueError('Input image must be 2D.')
    # Shape of the image with pixel weights
    nr, nc = flux.shape

    # Number of starting coordinates
    _cen = np.atleast_1d(start_cen)
    nt = _cen.size
    # Check coordinates are within the image
    if np.any((_cen > nc-1) | (_cen < 0)) or start_row < 0 or start_row > nr-1:
        raise ValueError('Starting coordinates incompatible with input image!')
    # Check the dimensionality
    if _cen.ndim != 1:
        raise ValueError('Input coordinates to be at most 1D.')

    # Instantiate output; just repeat input for all image rows.
    xc = np.tile(_cen, (nr,1)).astype(float)
    xe = np.zeros_like(xc, dtype=float)
    xm = np.zeros_like(xc, dtype=bool) if bitmask is None \
                else np.zeros_like(xc, dtype=bitmask.minimum_dtype())

    # Recenter the starting row
    i = start_row
    xc[i,:], xe[i,:], xm[i,:] = _recenter_trace_row(i, xc[i,:], flux, ivar, mask, width,
                                                    maxshift=maxshift_start, maxerror=maxerror,
                                                    bitmask=bitmask)

    # Go to higher indices using the result from the previous row
    for i in range(start_row+1,nr):
        xc[i,:], xe[i,:], xm[i,:] = _recenter_trace_row(i, xc[i-1,:], flux, ivar, mask, width,
                                                        maxshift=maxshift_follow,
                                                        maxerror=maxerror, bitmask=bitmask)

    # Go to lower indices using the result from the previous row
    for i in range(start_row-1,-1,-1):
        xc[i,:], xe[i,:], xm[i,:] = _recenter_trace_row(i, xc[i+1,:], flux, ivar, mask, width,
                                                       maxshift=maxshift_follow,
                                                       maxerror=maxerror, bitmask=bitmask)

    # TODO: Add a set of criteria the determine whether a trace is or is not continuous...

    # NOTE: In edgearr_tcrude, skip_bad (roughly opposite of continuous
    # here) was True by default.

    if not continuous:
        # Not removing discontinuous traces
        return xc, xe, xm

    # Keep only the continuous part of the trace starting from the
    # initial row
    # TODO: Instead keep the longest continuous segment?
    bad = xm > 0
    p = np.arange(nr)
    for i in range(nt):
        indx = bad[:,i] & (p > start_row)
        if np.any(indx):
            s = np.amin(p[indx])
            xm[s:,i] = True if bitmask is None else bitmask.turn_on(xm[s:,i], 'DISCONTINUOUS')
        indx = bad[:,i] & (p < start_row)
        if np.any(indx):
            e = np.amax(p[indx])-1
            xm[:e,i] = True if bitmask is None else bitmask.turn_on(xm[:e,i], 'DISCONTINUOUS')

    # Return centers, errors, and mask
    return xc, xe, xm


def _recenter_trace_row(row, cen, flux, ivar, mask, width, maxshift=None, maxerror=None,
                        bitmask=None, fill_error=-1):
    """
    Recenter the trace along a single row.

    This method is not meant for general use; it is a support method
    for :func:`follow_trace_moment`. The method executes
    :func:`recenter_moment` (with uniform weighting) for the
    specified row (spectral position) in the image, imposing a
    maximum difference with the input and centroid error, and asseses
    the result. The assessments are provided as either boolean flags
    or mask bits, depending on the value of `bitmask` (see below).

    Measurements flagged by :func:`recenter_moment` or if the
    centroid error is larger than `maxerror` (and `maxerror` is not
    None) are replaced by their input value.

    If `maxshift`, `maxerror`, and `bitmask` are all None, this is
    equivalent to::

        return recenter_moment(flux, cen, ivar=ivar, mask=mask, width=width, ycen=row,
                               fill_error=fill_error)

    Args:
        row (:obj:`int`):
            Row (index along the first axis; spectral position) in
            `flux` at which to recenter the trace position. See `ycen`
            in :func:`recenter_moment`.
        cen (`numpy.ndarray`_):
            Current estimate of the trace center.
        flux (`numpy.ndarray`_):
            Array used for the centroid calculations.
        ivar (`numpy.ndarray`_):
            Inverse variance in `flux`; passed directly to
            :func:`recenter_moment` and can be None.
        mask (`numpy.ndarray`_):
            Boolean mask for `flux`; passed directly to
            :func:`recenter_moment` and can be None.
        width (:obj:`float`):
            Passed directly to :func:`recenter_moment`; see the
            documentation there.
        maxshift (:obj:`float`, optional):
            Maximum shift allowed between the input and recalculated
            centroid.  If None, no limit is applied.
        maxerror (:obj:`flaot`, optional):
            Maximum error allowed in the calculated centroid.
            Measurements with errors larger than this value are
            returned as the input center value. If None, no limit is
            applied.
        bitmask (:class:`pypeit.bitmask.BitMask`, optional):
            Object used to toggle the returned bit masks. If
            provided, must be able to interpret MATHERROR,
            MOMENTERROR, and LARGESHIFT flags. If None, the function
            returns boolean flags set to True if there was an error
            in :func:`recenter_moment` or if the error is larger than
            `maxerror` (and `maxerror` is not None); centroids that
            have been altered by the maximum shift are *not* flagged.

    Returns:
        Returns three `numpy.ndarray`_ objects: the new centers, the
        center errors, and the measurement flags with a data type
        depending on `bitmask`.
    """
    xfit, xerr, matherr = recenter_moment(flux, cen, ivar=ivar, mask=mask, width=width,
                                          ycen=row, fill_error=fill_error)
    if maxshift is None and maxerror is None and bitmask is None:
        # Nothing else to do
        return xfit, xerr, matherr

    # Toggle the mask bits
    if bitmask is not None:
        xmsk = np.zeros_like(xfit, dtype=bitmask.minimum_dtype())
        xmsk[matherr] = bitmask.turn_on(xmsk[matherr], 'MATHERROR')
        if maxerror is not None:
            indx = xerr > maxerror
            xmsk[indx] = bitmask.turn_on(xmsk[indx], 'MOMENTERROR')
        if maxshift is not None:
            indx = np.absolute(xfit - cen) > maxshift
            xmsk[indx] = bitmask.turn_on(xmsk[indx], 'LARGESHIFT')

    # Impose the maximum shift
    if maxshift is not None:
        xfit = np.clip(xfit - cen, -maxshift, maxshift)+cen

    # Reset 'bad' values to the input
    indx = matherr
    if maxerror is not None:
        indx |= (xerr > maxerror)
    xfit[indx] = cen[indx]
    xerr[indx] = fill_error

    # Return the new centers, errors, and flags
    return xfit, xerr, indx if bitmask is None else xmsk


def fit_trace(flux, trace, order, ivar=None, mask=None, trace_mask=None, weighting='uniform',
              fwhm=3.0, function='legendre', maxdev=5.0, maxiter=25, niter=9, show_fits=False,
              idx=None, xmin=None, xmax=None):
    """
    Iteratively fit the trace of a feature in the provided image.

    Each iteration performs two steps:
        - Redetermine the trace data using :func:`recenter_moment`.
        The size of the integration window (see the definition of the
        `width` parameter for :func:`recenter_moment`)depends on the
        type of weighting: For *uniform weighting*, the code does a
        third of the iterations with window `width = 2*1.3*fwhm`, a
        third with `width = 2*1.1*fhwm`, and a third with `width =
        2*fwhm`. For *Gaussian weighting*, all iterations use `width
        = fwhm/2.3548`.

        - Fit the centroid measurements with a 1D function of the
        provided order. See :func:`pypeit.core.pydl.TraceSet`.

    The number of iterations performed is set by the keyword argument
    `niter`. There is no convergence test, meaning that this number
    of iterations is *always* performed.

    History:
        23-June-2018  Written by J. Hennawi

    Args:
        flux (`numpy.ndarray`_):
            Image to use for tracing. Must be 2D with shape (nspec,
            nspat).
        trace (`numpy.ndarray`_):
            Initial guesses for spatial direction trace. This can
            either be an 2-d array with shape (nspec, nTrace) array,
            or a 1-d array with shape (nspec) for the case of a
            single trace.
        order (:obj:`int`):
            Order of function to fit to each trace.  See `function`.
        ivar (`numpy.ndarray`_, optional):
            Inverse variance of the image intensity.  If not provided,
            unity variance is used.  If provided, must have the same
            shape as `flux`.
        mask (`numpy.ndarray`_, optional):
            Boolean array with the input mask for the image. If not
            provided, all values in `flux` are considered valid. If
            provided, must have the same shape as `flux`.
        trace_mask (`numpy.ndarray`_, optional):
            Boolean array with the trace mask; i.e., places where you
            know the trace is going to be bad that you always want to
            mask in the fits. Shape must match `trace`.
        weighting (:obj:`str`, optional):
            The weighting to apply to the position within each
            integration window (see :func:`recenter_moment`).
        fwhm (:obj:`float`, optional):
            The expected width of the feature to trace, which is used
            to define the size of the integration window during the
            centroid calculation; see description above.
        function (:obj:`str`, optional):
            The name of the function to fit. Must be a valid
            selection. See :class`pypeit.core.pydl.TraceSet`.
        maxdev (:obj:`float`, optional):
            If provided, reject points with `abs(data-model) >
            maxdev` during the fitting. If None, no points are
            rejected. See :func:`pypeit.utils.robust_polyfit_djs`.
        maxiter (:obj:`int`, optional):
            Maximum number of rejection iterations allowed during the
            fitting. See :func:`pypeit.utils.robust_polyfit_djs`.
        niter (:obj:`int`, optional):
            The number of iterations for this method; i.e., the
            number of times the two-step fitting algorithm described
            above is performed.
        show_fits (:obj:`bool`, optional):
            Plot the data and the fits.
        idx (`numpy.ndarray`_, optional):
            Array of strings with the IDs for each object. Used only
            if show_fits is true for the plotting. Default is just a
            running number.
        xmin (:obj:`float`, optional):
            Lower reference for robust_polyfit polynomial fitting.
            Default is to use zero
        xmax (:obj:`float`, optional):
            Upper refrence for robust_polyfit polynomial fitting.
            Default is to use the image size in nspec direction

    Returns:
        Returns four `numpy.ndarray`_ objects and the
        :class:`pypeit.core.pydl.TraceSet` object with the
        best-fitting polynomial parameters for the traces. The four
        `numpy.ndarray`_ objects all have the same shape as the input
        positions (`trace`) and provide::
            - The best-fitting positions of each trace determined by
            the polynomial fit.
            - The centroids of the trace determined by either flux-
            or Gaussian-weighting, to which the polynomial is fit.
            - The errors in the centroids.
            - Boolean flags for each centroid measurement (see
            :func:`recenter_moment`).
    """
    # Ensure setup is correct
    if flux.ndim != 2:
        raise ValueError('Input image must be 2D.')
    if ivar is None:
        ivar = np.ones_like(flux, dtype=float)
    if ivar.shape != flux.shape:
        raise ValueError('Inverse variance array shape is incorrect.')
    if mask is None:
        mask = np.zeros_like(flux, dtype=bool)
    if mask.shape != flux.shape:
        raise ValueError('Mask array shape is incorrect.')
    if trace_mask is None:
        trace_mask = np.zeros_like(trace, dtype=bool)

    # Allow for single vectors as input as well:
    _trace = trace.reshape(-1,1) if trace.ndim == 1 else trace
    _trace_mask = trace_mask.reshape(-1, 1) if trace.ndim == 1 else trace_mask
    nspec, ntrace = _trace.shape
    if _trace.shape != _trace_mask.shape:
        raise ValueError('Trace data and its mask do not have the same shape.')

    # Define the fitting limits
    if xmin is None:
        xmin = 0.0
    if xmax is None:
        xmax = float(nspec-1)

    # Abscissa for fitting; needs to be float type when passed to
    # TraceSet
    trace_coo = np.tile(np.arange(nspec), (ntrace,1)).astype(float)

    # Setup the width to use for each iteration depending on the weighting used
    width = np.full(niter, 2*fwhm if weighting == 'uniform' else fwhm/2.3548, dtype=float)
    if weighting == 'uniform':
        width[:niter//3] *= 1.3
        width[niter//3:2*niter//3] *= 1.1

    trace_fit = np.copy(_trace)
    # Uniform weighting during the fit
    trace_fit_ivar = np.ones_like(trace_fit)

    for i in range(niter):
        # First recenter the trace using the previous trace fit/data
        trace_cen, trace_err, bad_trace = recenter_moment(flux, trace_fit, ivar=ivar, mask=mask,
                                                          weighting=weighting, width=width[i])

        # TODO: Update trace_mask with bad_trace?

        # Do not do any kind of masking based on the trace recentering
        # errors. Trace fitting is much more robust when masked pixels
        # are simply replaced by the input trace values.
        
        # Do not do weighted fits, i.e. uniform weights but set the
        # error to 1.0 pixel
        traceset = pydl.TraceSet(trace_coo, trace_cen.T, inmask=np.invert(_trace_mask.T),
                                 function=function, ncoeff=order, maxdev=maxdev, maxiter=maxiter,
                                 invvar=trace_fit_ivar.T, xmin=xmin, xmax=xmax)

        # TODO: Report iteration number and mean/stddev in difference
        # of coefficients with respect to previous iteration
        trace_fit = traceset.yfit.T

    # Plot the final fit if requested
    if show_fits:
        # Set the title based on the type of weighting used
        title_text = 'Flux Weighted' if weighting == 'uniform' else 'Gaussian Weighted'
        if idx is None:
            idx = np.arange(1,ntrace+1).astype(str)

        # Bad pixels have errors set to 999 and are returned to lie on
        # the input trace. Use this only for plotting below.
        for i in range(ntrace):
            plt.scatter(trace_coo[i,:], trace_cen[:,i], marker='o', color='k', s=30,
                        label=title_text + ' Centroid')
            plt.plot(trace_coo[i,:], _trace[:,i], color='g', zorder=25, linewidth=2.0,
                     linestyle='--', label='initial guess')
            plt.plot(trace_coo[i,:], trace_fit[:,i], c='red', zorder=30, linewidth=2.0,
                     label ='fit to trace')
            if np.any(bad_trace[:,i]):
                plt.scatter(trace_coo[i,bad_trace[:,i]], trace_fit[bad_trace[:,i],i], c='blue',
                            marker='+', s=50, zorder=20, label='masked points, set to init guess')
            if np.any(_trace_mask[:,i]):
                plt.scatter(trace_coo[i,_trace_mask[:,i]], trace_fit[_trace_mask[:,i],i],
                            c='orange', marker='s', s=30, zorder=20,
                            label='input masked points, not fit')

            plt.title(title_text + ' Centroid to object {0}.'.format(idx[i]))
            plt.ylim((0.995*np.amin(trace_fit[:,i]), 1.005*np.amax(trace_fit[:,i])))
            plt.xlabel('Spectral Pixel')
            plt.ylabel('Spatial Pixel')
            plt.legend()
            plt.show()

    # Returns the fit, the actual weighted traces and flag, and the TraceSet object
    return trace_fit, trace_cen, trace_err, bad_trace, traceset


def pca_trace(trace, predict=None, npca=None, pca_explained_var=99.0, coeff_npoly=None,
              debug=False, trace_coo=None, lower=3.0, upper=3.0, minv=None, maxv=None, maxrej=1,
              trace_mean=None):

    r"""
    Perform principle-component analysis (PCA) of a set of trace
    coordinates.

    This function is used in pypeit to both analyze slit edge traces
    and object traces between different echelle orders.

    First, all valid traces (see `predict`) are passed to an
    unconstrained PCA to determine the growth curve of the accounted
    variance as a function of PCA component. If specifying a number
    of PCA components to use (see `npca`), this yields the percentage
    of the variance accounted for in the analysis. If instead
    specifying the target variance percentage (see
    `pca_explained_var`, this is used to determine the number of PCA
    components to use in the finaly analysis.

    The PCA is then recomputed with the limited number of components.
    The coefficients of each component for reach trace is then fit by
    a low-order polynomial (see `trace_coo`, `coeff_npoly`, and other
    fitting parameters that are passed to
    :func:`pypeit.utils.robust_polyfit_djs`).

    The final PCA-determined trace positions are determined using the
    coefficients sampled from the fitted polynomial. These
    coefficients are used both for the traces used in the PCA and to
    construct any predicted traces requested (see `predict`).

    Args:
        trace (`numpy.ndarray`_):
            Trace coordinate data to analyze. This must be a 2-d
            array with shape (nspec, ntrace) array.
        predict (`numpy.ndarray`_, optional):
            Boolean array with flags of traces in `trace` that should
            be predicted based on the PCA of the other traces. Must
            have shape (ntrace,). If None, the function determines
            the PCA coefficients using all traces with no no
            extrapolation to predict new traces. When used for object
            finding, we use the standard star (or slit boundaries) as
            the input for orders for which a trace is not identified
            and fit the coefficients of all simultaneously (no
            extrapolation is performed). For tracing slit boundaries
            it may be useful to perform extrapolations.
        npca (:obj:`bool`, optional):
            The number of PCA components to keep, which must be less
            than ntrace. If `npca==ntrace`, no PCA compression
            occurs. If None, `npca` is automatically determined by
            calculating the minimum number of components required to
            explain a given percentage of variance with respect to
            the trace data (see `pca_explained_var`).
        pca_explained_var (:obj:`float`, optional):
            The percentage (i.e., not the fraction) of the variance
            in the data accounted for by the PCA used to truncate the
            number of PCA coefficients to keep (see `npca`). Ignored
            if `npca` is provided directly.
        coeff_npoly (:obj:`int`, optional):
            Order of polynomial fits used for PCA coefficients
            fitting. If None, the polynomial order is determined as
            follows::

                coeff_npoly = int(np.fmin(np.fmax(np.floor(3.3*ngood/ntrace),1.0),3.0))

            where `ngood` is the number of traces used to construct
            the PCA (number of False values in `predict`) and
            `ntrace` is the total number of traces. In general, PCA
            components that explain less variance (and are thus much
            noiser) are fit with lower order. In the limit where all
            traces are used in the PCA, the polynomial order is 3.
            TODO: This is wrong!  (see npoly in the function)
        debug (:obj:`bool`, optional):
            Show plots useful for debugging.
        trace_coo (`numpy.ndarray`_, optional):
            Floating-point array with the independent coordinates to
            use when fitting the PCA coefficients. If None, simply
            uses a running number.  Shape must be (ntrace,).
        lower (:obj:`float`, optional):
            Number of standard deviations used for rejecting data
            **below** the mean residual during the coefficient
            fitting. If None, no rejection is performed. See
            :func:`utils.robust_polyfit_djs`.
        upper (:obj:`float`, optional):
            Number of standard deviations used for rejecting data
            **above** the mean residual during the coefficient
            fitting. If None, no rejection is performed. See
            :func:`utils.robust_polyfit_djs`.
        minv, maxv (:obj:`float`, optional):
            Minimum and maximum values used to rescale the
            independent axis data during the coefficient fitting. If
            None, the minimum and maximum values of `trace_coo` are
            used. See `minx` and `maxx` in
            :func:`utils.robust_polyfit_djs`.
        maxrej (:obj:`int`, optional):
            Maximum number of points to reject during fit iterations.
            See :func:`utils.robust_polyfit_djs`.
        trace_mean (`numpy.ndarray`_, optional):
            The mean position of each trace to subtract from the
            trace data before performing the PCA. If None, this is
            just the mean position of each trace for all spectral
            pixels. Shape must be (ntrace,).

    Returns:
        Four objects: pca_fit, fit_dict, pca.mean_, pca_vectors.
        TODO: Need to explain these.... The first one is: Array with
        the same size as xinit, which contains the pca fitted orders.
    """
    # Check input
    if trace.ndim != 2:
        raise ValueError('Input trace data must be a 2D array')
    nspec, ntrace = trace.shape
    if trace_coo is None:
        trace_coo = np.arange(ntrace, dtype=float)
    if trace_coo.size != ntrace:
        raise ValueError('Trace coordinates has incorrect shape.')

    if predict is None:
        predict = np.zeros(ntrace, dtype=bool)
    if predict.size != ntrace:
        raise ValueError('Input predict vector has incorrect length.')

    # Set of good traces to use to predict bad traces
    use_trace = np.invert(predict)
    ngood = np.sum(use_trace)
    if ngood < 2:
        raise ValueError('The must be at least 2 valid traces for the PCA analysis.')

    # Take out the mean position of each input trace
    if trace_mean is None:
        # TODO: replace this default with most_common_trace_row?
        trace_mean = np.mean(trace, axis=0)

    # TODO: Why aren't trace_mean and trace_coo always the same?

    # Below is allowed because of numpy broadcasting. trace has shape
    # (nspec,ntrace) and trace_mean has shape (ntrace,); trace_mean is
    # subtracted from each row of trace.  This is equivalent to::
    #   trace_pca = trace - trace_mean[None,:]
    trace_pca = trace - trace_mean

    # Perform unconstrained PCA of the valid traces
    pca = PCA()
    pca.fit(trace_pca[:,use_trace].T)

    # Compute the cumulative distribution of the variance explained by the PCA components.
    # TODO: Why round to 6 decimals?  Why work in percentages?
    var = np.cumsum(np.round(pca.explained_variance_ratio_, decimals=6) * 100)
    # Number of components for a full decomposition
    npca_tot = var.size

    print('The unconstrained PCA yields {0} components.'.format(npca_tot))

    if npca is None:
        # Assign the number of components to use based on the variance
        # percentage
        if pca_explained_var is None:
            raise ValueError('Must provide percentage explained variance.')
        npca = int(np.ceil(np.interp(pca_explained_var, var, np.arange(npca_tot)+1))) \
                    if var[0] < pca_explained_var else 1
    elif npca_tot < npca:
        raise ValueError('Not enough good traces for a PCA fit of the requested dimensionality.  '
                         'The full (uncompressing) PCA has {0} components'.format(npca_tot)
                         + ', which is less than the requested {0} components.'.format(npca)
                         + '  Lower the number of requested PCA components or turn off the PCA.')

    print('PCA will include {0} components, containing {1:.3f}'.format(npca, var[npca-1])
              + '% of the total variance.')

    # Determine the PCA coefficients with the revised number of components
    pca = PCA(n_components=npca)
    pca_coeffs_use = pca.fit_transform(trace_pca[:,use_trace].T)

    # Fit the coefficients with a polynomial: Order is set to cascade
    # down to lower order for components that account for a smaller
    # percentage of the variance.
    if coeff_npoly is None:
        coeff_npoly = int(np.fmin(np.fmax(np.floor(3.3*ngood/ntrace),1.0),3.0))
    ncoeff = np.clip(coeff_npoly - np.arange(npca), 1, None).astype(int)

    # Initialize objects for output data
    pca_coeffs_new = np.zeros((ntrace, npca), dtype=float)
    fit_dict = {}

    # Now loop over the dimensionality of the compression and fit
    # polynomials to the coefficients as a function of trace coordinate
    for i in range(npca):
        # TODO: robust_poly_fit needs to return minv and maxv as
        # outputs for the fits to be usable downstream
        # TODO: Why a nominal polynomial and not a Legendre polynomial?
        # Only fit the use_trace orders ...
        msk_new, poly_out = utils.robust_polyfit_djs(trace_coo[use_trace], pca_coeffs_use[:,i],
                                                     ncoeff[i], function='polynomial', maxiter=25,
                                                     lower=lower, upper=upper, maxrej=maxrej,
                                                     sticky=False, minx=minv, maxx=maxv)
        # ... and use the result to predict the coefficients for all traces
        pca_coeffs_new[:,i] = utils.func_val(poly_out, trace_coo, 'polynomial')

        # Save the results
        # TODO: Just return the objects...
        fit_dict[i] = {}
        fit_dict[i]['coeffs'] = poly_out
        fit_dict[i]['minv'] = minv
        fit_dict[i]['maxv'] = maxv

        if debug:
            # Visually check the fits
            xvec = np.linspace(trace_coo.min(),trace_coo.max(),num=100)
            robust_rejected = np.invert(msk_new == 1)
            plt.plot(trace_coo[use_trace], pca_coeffs_use[:,i], 'ko', mfc='None', markersize=8.0,
                     label='pca coeff')
            if np.any(robust_rejected):
                plt.plot(trace_coo[use_trace][robust_rejected],
                         pca_coeffs_use[:,i][robust_rejected], 'r+',
                         markersize=20.0, label='robust_polyfit_djs rejected')
            plt.plot(xvec, utils.func_val(poly_out, xvec, 'polynomial'), ls='-.',
                     color='steelblue', label='Polynomial fit of order={0}'.format(ncoeff[i]))
            plt.xlabel('Trace Coordinate', fontsize=14)
            plt.ylabel('PCA Coefficient', fontsize=14)
            plt.title('PCA Fit for Dimension #{0}/{1}'.format(i+1, npca))
            plt.legend()
            plt.show()

    # Construct the PCA driven traces; allowed because of numpy
    # broadcasting rules for matrices and vectors.
    trace_pca = (np.dot(pca_coeffs_new, pca.components_) + pca.mean_).T + trace_mean

    # Return the results
    return trace_pca, poly_out, minv, maxv, pca


def build_trace_mask(flux, trace, mask=None, boxcar=None, thresh=None, median_kernel=None):
    """
    Construct a trace mask.

    If no keyword arguments are provided, the traces are only masked
    when they land outside the bounds of the image.

    If both `boxcar` and `thresh` are provided, traces are also
    masked by extracting the provided image along the trace and
    flagging extracted values below the provided threshold.

    Args:
        flux (`numpy.ndarray`_):
            Image to use for tracing. Shape is expected to be (nspec,
            nspat).
        trace (`numpy.ndarray`_):
            Trace locations. Can be a 1D array for a single trace or a
            2D array with shape (nspec, ntrace) for multiple traces.
        mask (`numpy.ndarray`_, optional):
            Boolean array with the input mask for the image. If not
            provided, all values in `flux` are considered valid. If
            provided, must have the same shape as `flux`.
        boxcar (:obj:`float`, optional):
            The width of the extraction window used for all traces
            and spectral rows. If None, the trace mask will not
            consider the extracted flux.
        thresh (:obj:`float`, optional):
            The minimum valid value of the extraced flux used to mask
            the traces. If None, the trace mask will not consider the
            extracted flux.
        median_kernel (:obj:`int`, optional):
            The spectral width of the kernel to use with
            `scipy.signal.medfilt` to filter the *extracted* data
            before setting the trace mask based on the provided
            threshold. If None, the extracted data are not filtered
            before flagging data below the threshold.

    Returns:
        `numpy.ndarray`_: The boolean mask for the traces.
    """
    # Setup and ensure input is correct
    if flux.ndim != 2:
        raise ValueError('Input image must be 2D.')
    nspec, nspat = flux.shape
    if mask is None:
        mask = np.zeros_like(flux, dtype=bool)
    if mask.shape != flux.shape:
        raise ValueError('Mask array shape is incorrect.')
    _trace = trace.reshape(-1,1) if trace.ndim == 1 else trace
    if _trace.shape[0] != nspec:
        raise ValueError('Must provide trace position for each spectral pixel.')

    # Flag based on the trace positions
    trace_mask = (_trace < 0) | (_trace > nspat - 1)

    if boxcar is None or thresh is None:
        # Only flagging based on the trace positions
        return trace_mask

    # Get the extracted flux
    extract_flux = extract_boxcar(flux, _trace, boxcar, mask=mask)[0]
    if median_kernel is not None:
        # Median filter the extracted data
        extract_flux = signal.medfilt(extract_flux, kernel_size=(median_kernel,1))
    return trace_mask | (extract_flux < thresh)


# TODO: Add an option where the user specifies the number of slits, and
# so it takes only the highest peaks from detect_lines
def peak_trace(flux, trace, ivar=None, mask=None, trace_mask=None, function='legendre', order=5,
               npca=None, pca_explained_var=99.8, coeff_npoly_pca=3, fwhm_gaussian=3.0,
               fwhm_uniform=3.0, peak_thresh=100.0, trace_thresh=10.0, trace_median_frac=0.01,
               lower=2.0, upper=2.0, maxrej=1, smash_range=None, trough=False, debug=False):
    """
    Trace features by finding peaks in a rectified image collapsed
    along the spectral axis.

    Rectification of the image is based on a PCA analysis of the
    input traces; see :func:`pca_trace`. Specifically, *all* provided
    traces are used to constrain the PCA, and then the PCA is used to
    predict the traces that would cross through *every* spatial pixel
    for a given spectral row. Based on this detector to trace
    mapping, the input image is rectified by boxcar extraction along
    the trace predicted for each spatial pixel.

    The rectified image is then collapsed spectrally (see
    `smash_range`) giving the sigma-clipped mean flux as a function
    of spatial position. Peaks are then isolated in this vector (see
    :func:`pypeit.core.arc.detect_lines`).

    PCA-based traces that pass through these peak positions are then
    passed to two iterations of :func:`fit_trace`, which both
    remeasures the centroids of the trace and fits a polynomial to
    those trace data. The first iteration determines the centroids
    with uniform weighting, passing `fwhm=fwhm_uniform` to
    :func:`fit_trace`, and the second uses Gaussian weighting for the
    centroid measurements (passing `fwhm=fwhm_gaussian` to
    :func:`fit_trace`).

    The returned data are the fitted traces resulting from the final
    call to :func:`fit_trace`; the output from :func:`fit_trace` are
    returned directly.

    Args:
        flux (`numpy.ndarray`_):
            Image to use for tracing.
        trace (`numpy.ndarray`_):
            Current traces. Can be a 1D array for a single trace or a
            2D array with shape (nspec, ntrace) for multiple traces.
        ivar (`numpy.ndarray`_, optional):
            Inverse variance of the image intensity.  If not provided,
            unity variance is used.  If provided, must have the same
            shape as `flux`.
        mask (`numpy.ndarray`_, optional):
            Boolean array with the input mask for the image. If not
            provided, all values in `flux` are considered valid. If
            provided, must have the same shape as `flux`.
        trace_mask (`numpy.ndarray`_, optional):
            Boolean array with the trace mask; i.e., places where you
            know the trace is going to be bad that you always want to
            mask in the fits. Shape must match `trace`.
        function (:obj:`str`, optional):
            The type of polynomial to fit to the trace data. See
            :func:`fit_trace`.
        order (:obj:`int`, optional):
            Order of the polynomial to fit to each trace.
        npca (:obj:`bool`, optional):
            The number of PCA components to keep, which must be less
            than ntrace. If `npca==ntrace`, no PCA compression
            occurs. If None, `npca` is automatically determined by
            calculating the minimum number of components required to
            explain a given percentage of variance with respect to
            the trace data (see `pca_explained_var`). See
            :func:`pca_trace`.
        pca_explained_var (:obj:`float`, optional):
            The percentage (i.e., not the fraction) of the variance
            in the data accounted for by the PCA used to truncate the
            number of PCA coefficients to keep (see `npca`). Ignored
            if `npca` is provided directly. See :func:`pca_trace`.
        coeff_npoly_pca (:obj:`int`, optional):
            Order of polynomial fits used for PCA coefficients
            fitting. If None, the polynomial order is determined as
            follows::

                coeff_npoly_pca = int(np.fmin(np.fmax(np.floor(3.3*ngood/ntrace),1.0),3.0))

            where `ngood` is the number of traces used to construct
            the PCA (number of False values in `predict`) and
            `ntrace` is the total number of traces. In general, PCA
            components that explain less variance (and are thus much
            noiser) are fit with lower order. In the limit where all
            traces are used in the PCA, the polynomial order is 3.
            TODO: This is wrong!  (see npoly in the function)
            See `coeff_npoly` argument for :func:`pca_trace`.
        fwhm_uniform (:obj:`float`, optional):
            The `fwhm` parameter to use when using uniform weighting
            in the calls to :func:`fit_trace`. See description of the
            algorithm above. TODO: ADD THIS.
        fwhm_gaussian (:obj:`float`, optional):
            The `fwhm` parameter to use when using Gaussian weighting
            in the calls to :func:`fit_trace`. See description of the
            algorithm above. TODO: ADD THIS.
        peak_thresh (:obj:`float, optional):
            The threshold for detecting peaks in the image. See the
            `input_thresh` parameter for
            :func:`pypeit.core.arc.detect_lines`.
        trace_thresh (:obj:`float`, optional):
            After rectification and median filtering of the image
            (see `trace_median_frac`), values in the resulting image
            that are *below* this threshold are masked in the
            refitting of the trace using :func:`fit_trace`.
        trace_median_frac (:obj:`float`, optional):
            After rectification of the image and before refitting the
            traces, the rectified image is median filtered with a
            kernel width of trace_median_frac*nspec along the
            spectral dimension (TODO: CHECK THIS).
        lower (:obj:`float`, optional):
            Number of standard deviations used for rejecting data
            **below** the mean residual during the coefficient
            fitting. If None, no rejection is performed. See
            :func:`utils.robust_polyfit_djs`.
        upper (:obj:`float`, optional):
            Number of standard deviations used for rejecting data
            **above** the mean residual during the coefficient
            fitting. If None, no rejection is performed. See
            :func:`utils.robust_polyfit_djs`.
        maxrej (:obj:`int`, optional):
            Maximum number of points to reject during fit iterations.
            See :func:`utils.robust_polyfit_djs`.
        smash_range (:obj:`tuple`, optional):
            Spectral range to over which to collapse the input image
            into a 1D flux as a function of spatial position. This 1D
            vector is used to detect features for tracing. This is
            useful (and recommended) for definining the relevant
            detector range for data with spectra that do not span the
            length of the detector. The tuple gives the minimum and
            maximum in the fraction of the full spectral length
            (nspec). If None, the full image is collapsed.
        trough (:obj:`bool`, optional):
            Trace both peaks **and** troughs in the input image. This
            is done by flipping the value of the smashed image about
            its median value, such that troughs can be identified as
            peaks.
        debug (:obj:`bool`, optional):
            Show plots useful for debugging.

    Returns:
        TODO: Copy return from fit_trace

    """
    # Setup and ensure input is correct
    if flux.ndim != 2:
        raise ValueError('Input image must be 2D.')
    nspec, nspat = flux.shape
    if ivar is None:
        ivar = np.ones_like(flux, dtype=float)
    if ivar.shape != flux.shape:
        raise ValueError('Inverse variance array shape is incorrect.')
    if mask is None:
        mask = np.zeros_like(flux, dtype=bool)
    if mask.shape != flux.shape:
        raise ValueError('Mask array shape is incorrect.')
    if trace_mask is None:
        trace_mask = np.zeros_like(trace, dtype=bool)

    # Allow for single vectors as input as well
    _trace = trace.reshape(-1,1) if trace.ndim == 1 else trace
    _trace_mask = trace_mask.reshape(-1, 1) if trace.ndim == 1 else trace_mask
    ntrace = _trace.shape[1]
    if _trace.shape != _trace_mask.shape:
        raise ValueError('Trace data and its mask do not have the same shape.')
    if _trace.shape[0] != nspec:
        raise ValueError('Must provide trace position for each spectral pixel.')

    # Define the region to collapse
    if smash_range is None:
        smash_range = (0,1)

    # Define the reference spatial positions based on the spectral row
    # that crosses the most traces. When the flux image is rectified,
    # the traces should line up with these positions.
    trace_ref_row = most_common_trace_row(_trace_mask)
    trace_ref = _trace[trace_ref_row,:]
#    print(trace_ref)

    # Construct a full trace set to pass to pca_trace with one trace
    # per spatial column; only the valid traces are used to construct
    # the PCA and then the PCA is used to interpolate/extrapolate the
    # trace to each spatial position.
    trace_full = np.tile(np.arange(nspat).astype(float), (nspec,1))
    trace_full[:,np.round(trace_ref).astype(int)] = _trace
    valid_trace = np.zeros(nspat, dtype=bool)
    valid_trace[np.round(trace_ref).astype(int)] = True
    trace_full_ref = trace_full[trace_ref_row,:]
#    print(trace_full[trace_ref_row,valid_trace])

    # Model all traces, ignoring if they're left or right traces
    msgs.info('Running PCA on {0} trace(s)'.format(ntrace))
    trace_pca, _, _, _, _ \
            = pca_trace(trace_full, predict=np.invert(valid_trace), npca=npca,
                        pca_explained_var=pca_explained_var, coeff_npoly=coeff_npoly_pca,
                        debug=debug, trace_coo=trace_full_ref, lower=lower, upper=upper, minv=0.0,
                        maxv=float(nspec-1), maxrej=maxrej, trace_mean=trace_full_ref)
#    print(trace_pca[trace_ref_row,valid_trace])
#    print(trace_full[trace_ref_row,valid_trace] - trace_pca[trace_ref_row,valid_trace])
#
#    indx = np.where(valid_trace)[0]
#    plt.scatter(np.arange(nspec), trace_full[:,indx[5]], marker='.', color='k', s=40, lw=0)
#    plt.scatter(np.arange(nspec), trace_pca[:,indx[5]], marker='.', color='C3', s=20, lw=0)
#    plt.scatter(np.arange(nspec), trace_pca[:,indx[5]+1], marker='.', color='C1', s=20, lw=0)
#    plt.show()

    msgs.info('Extracting image along traces')
    # TODO: JFH What should this aperture size be? I think fwhm=3.0
    # since that is the width of the sobel filter

#    from pypeit.core import extract as ext
#    t = time.perf_counter()
#    flux_extract_0 = ext.extract_asymbox2(flux, trace_pca - fwhm_gaussian/2.0,
#                                            trace_pca + fwhm_gaussian/2.0)
#    print(time.perf_counter() - t)
#    print(flux_extract_0.shape)
#    t = time.perf_counter()
    flux_extract = extract(flux, trace_pca-1, trace_pca+1, ivar=ivar, mask=mask)[0]
#    print(time.perf_counter() - t)

#    plt.imshow(flux, origin='lower', interpolation='nearest', aspect='auto')
#    for t in _trace.T:
#        plt.plot(t, np.arange(4096), color='C3', lw=0.5, zorder=4)
#    plt.colorbar()
#    plt.show()

#    plt.imshow(flux_extract, origin='lower', interpolation='nearest', aspect='auto')
#    for t in trace_full_ref[valid_trace]:
#        plt.plot([t,t], [0,4095], color='C3', lw=0.5, zorder=4)
#    plt.colorbar()
#    plt.show()


#    if debug:
#        ginga.show_image(flux_extract, chname ='rectified image')

    # Collapse the image along the spectral direction to isolate the peaks to trace
    start, end = np.asarray(smash_range).astype(int)*nspec
    flux_smash_mean, flux_smash_median, flux_smash_sig \
            = sigma_clipped_stats(flux_extract[start:end,:], axis=0, sigma=4.0)

    # Offset by the median
    # TODO: If tracing Sobel-filtered image, this should be close (or
    # identically?) 0
    flux_median = np.median(flux_smash_mean)
    flux_smash_mean -= flux_median

    # Trace peak or both peaks and troughs
    label = ['peak', 'trough'] if trough else ['peak']
    sign = [1, -1] if trough else [1]

    npeak = 0
    trace_fit = np.empty((nspec,0), dtype=float)
    trace_cen = np.empty((nspec,0), dtype=float)
    trace_err = np.empty((nspec,0), dtype=float)
    bad_trace = np.empty((nspec,0), dtype=bool)

    # Get the smoothing kernel, ensuring the width is odd
    median_kernel = int(np.ceil(nspec*trace_median_frac))//2 * 2 + 1

    for i,(l,s) in enumerate(zip(label,sign)):

        # Identify the peaks in the rectified, collapsed image
        _, _, cen, _, _, best, _, _ \
                = arc.detect_lines(s*flux_smash_mean, cont_subtract=False, fwhm=fwhm_gaussian,
                                   input_thresh=peak_thresh, max_frac_fwhm=4.0,
                                   min_pkdist_frac_fwhm=5.0, debug=debug)
        if len(cen) == 0 or not np.any(best):
            continue
        print('Found {0} good {1}(s) in the rectified, collapsed image'.format(len(cen[best]),l))

        # As the starting point for the iterative trace fitting, use
        # the PCA trace results at the positions of the detected peaks
        loc = np.round(cen[best]).astype(int) 
        trace_peak = trace_pca[:,loc]

        plt.scatter(np.tile(np.arange(nspec), (ntrace,1)).T, _trace,
                    marker='.', color='k', s=80, lw=0)
        plt.scatter(np.tile(np.arange(nspec), (trace_peak.shape[1],1)).T, trace_peak,
                    marker='.', color='C3', s=40, lw=0)
        plt.show()

        # Image to trace; flip when tracing the troughs, set the
        # minimum allowed in the image based on the peak detection
        # threshold
        # TODO: This -1 is drawn out of the ether
        _flux = np.maximum(s*(flux - flux_median), -1)

        # Construct the trace mask
        trace_peak_mask = build_trace_mask(_flux, trace_peak, mask=mask, boxcar=fwhm_gaussian,
                                           thresh=trace_thresh, median_kernel=median_kernel)

        # Remeasure and fit the trace using uniform weighting
        trace_peak, cen, err, bad, _ \
                = fit_trace(_flux, trace_peak, order, ivar=ivar, mask=mask,
                            trace_mask=trace_peak_mask, fwhm=fwhm_uniform, function=function,
                            niter=9, show_fits=True)

        plt.scatter(np.tile(np.arange(nspec), (ntrace,1)).T, _trace,
                    marker='.', color='k', s=80, lw=0)
        plt.scatter(np.tile(np.arange(nspec), (trace_peak.shape[1],1)).T, trace_peak,
                    marker='.', color='C3', s=40, lw=0)
        plt.show()

        # Reset the mask
        # TODO: Use bad_trace
        trace_peak_mask = build_trace_mask(_flux, trace_peak, mask=mask, boxcar=fwhm_gaussian,
                                           thresh=trace_thresh, median_kernel=median_kernel)

        # Redo the measurements and trace fitting with Gaussian
        # weighting
        trace_peak, cen, err, bad, _ \
                = fit_trace(_flux, trace_peak, order, ivar=ivar, mask=mask,
                            trace_mask=trace_peak_mask, weighting='gaussian', fwhm=fwhm_gaussian,
                            function=function, niter=6, show_fits=True)

        plt.scatter(np.tile(np.arange(nspec), (ntrace,1)).T, _trace,
                    marker='.', color='k', s=80, lw=0)
        plt.scatter(np.tile(np.arange(nspec), (trace_peak.shape[1],1)).T, trace_peak,
                    marker='.', color='C3', s=40, lw=0)
        plt.show()

        trace_fit = np.append(trace_fit, trace_peak, axis=1)
        trace_cen = np.append(trace_cen, cen, axis=1)
        trace_err = np.append(trace_err, err, axis=1)
        bad_trace = np.append(bad_trace, bad, axis=1)

        if i == 0:
            # Save the number of peaks (troughs are appended, if they're located)
            npeak = cen.shape[1]

    exit()

    return trace_fit, trace_cen, trace_err, bad_trace, npeak

def extract_boxcar(flux, xcen, width, ivar=None, mask=None, wgt=None, ycen=None, fill_error=-1.):
    """
    Simple wrapper for :func:`extract` that sets the left and right
    locations of the extraction aperture based on the provided center
    and width.

    The arguments `xcen` and `width` must be able to broadcast to the
    correct length for the input of `left` and `right` to
    :func:`extract`.
    """
    return extract(flux, xcen-width/2, xcen+width/2, ivar=ivar, mask=mask, wgt=wgt, ycen=ycen,
                  fill_error=fill_error)

def extract(flux, left, right, ivar=None, mask=None, wgt=None, ycen=None, fill_error=-1.):
    r"""
    Extract the flux within a set of apertures.

    This function is very similar to :func:`recenter_moment`, except
    that the return values are the weighted zeroth moment, not the
    weighted first moment.

    Although the input image must be 2D, the calculation is 1D along
    its second axis (axis=1). Specifically, the extracted flux
    (:math:`\mu`) and flux errors (:math:`\epsilon_\mu`) are

    .. math::

        \mu &= \sum_i w_i f_i \\
        \epsilon_\mu^2 &= \sum_i (w_i \epsilon_{f,i})^2

    where :math:`f` is the flux in each pixel and :math:`\epsilon_f`
    is its error.

    The method uses numpy masked arrays to keep track of errors in
    the calculation, likely due to math errors (e.g., divisions by
    0). The returned boolean array indicates when these errors
    occurred.

    .. todo::
        - Method sets masked values to have weight = 0. This feels
        non-ideal, but there may not be a more meaningful approach...
        - Consolidate this function with :func:`recenter_moment` into
        a single function (e.g., calculate_moment)?
        - This could also be used for optimal extraction by just
        changing the weighting...

    Args:
        flux (`numpy.ndarray`_):
            Intensity image with shape (ny, nx).
        left (`numpy.ndarray`_):
            Floating-point pixel coordinate along the 2nd axis of the
            image for the left edge of the extraction aperture. This
            can either be an 2-d array with shape (ny, na) array, or
            a 1-d array with shape (na), where na is the number of
            apertures along the 2nd axis to extract.
        right (`numpy.ndarray`_):
            Similar definition as `left` but for the right edge of
            the extraction aperture. Must have the same shape as
            `left`.
        ivar (`numpy.ndarray`_, optional):
            Inverse variance of the image intensity.  If not provided,
            unity variance is used.  If provided, must have the same
            shape as `flux`.
        mask (`numpy.ndarray`_, optional):
            Mask for the input image.  True values are ignored, False
            values are included.  If not provided, all pixels are
            included.  If provided, must have the same shape as
            `flux`.
        wgt (`numpy.ndarray`_, optional):
            Weight to apply to each pixel in the extraction. If None,
            each pixel is given unity weight, except for mask pixels
            (if defined, given 0 weight). If provided, must have the
            same shape as `flux`.
        ycen (:obj:`int`, `numpy.ndarray`_, optional):
            Integer or integer array with the position along the 0th
            axis for the extraction. If None, assume `np.arange(ny)`
            for each aperture (na). If a single number, assume the
            value is identical for all apertures. Otherwise, must
            have the same shape as `left` and `right`.
        fill_error (:obj:`float`, optional):
            Value to use as filler for undetermined fluxes, resulting
            from either the input mask or computational issues
            (division by zero, etc.; see return description below).

    Returns:
        Three `numpy.ndarray`_ objects are returned, all with the
        same shape as the input aperture coordinates (e.g., `left`)::
            - The flux within the aperture along the 2nd axis of the
            input image. Masked values are set to 0.
            - The formal propagated error (see equation above) in the
            flux. Errors are only meaningful if `ivar` is provided.
            Masked values (indicated by the third object returned)
            are set to `fill_error`.
            - A boolean mask for output data; True values should be
            ignored, False values are valid measurements.

    Raises:
        ValueError:
            Raised if input shapes are not correct.
    """

    # TODO: There's a lot of setup overhead to make this a general
    # function. If multiple calls to the function are expected, should
    # gain by only doing this setup once for a given image...
    
    # Check the input images
    _ivar = np.ones_like(flux, dtype=float) if ivar is None else ivar
    if _ivar.shape != flux.shape:
        raise ValueError('Inverse variance must have the same shape as the input image.')
    _mask = np.zeros_like(flux, dtype=bool) if mask is None else mask
    if _mask.shape != flux.shape:
        raise ValueError('Pixel mask must have the same shape as the input image.')
    _wgt = np.ones_like(flux, dtype=float) if wgt is None else wgt
    if _wgt.shape != flux.shape:
        raise ValueError('Pixel weights must have the same shape as the input image.')

    # Process coordinate input
    ny, nx = flux.shape
    _left = np.atleast_1d(left)       # If entered as a numpy array, the array is not a copy
    ndim = _left.ndim
    if ndim > 2:
        raise ValueError('Aperture array must be 1 or 2 dimensional')
    dim = _left.shape
    npix = dim[0]
    if dim == 2 and npix > ny:
        # TODO: Is this test necessary?
        raise ValueError('More locations to trace ({0}) than pixels in the image ({1})!'.format(
                            npix,ny))
    nap = 1 if ndim == 1 else dim[1]

    _right = np.atleast_1d(right)   # If entered as a numpy array, the array is not a copy
    if _right.shape != _left.shape:
        raise ValueError('Right aperture edge must match the shape of the left aperture edge.')

    # Copy and normalize the shape of the input (flatten creates a
    # copy); the width needs to be an array for the compressed
    # calculation below.
    _left = _left.flatten()
    _right = _right.flatten()

    # Set the row locations for the recentering
    # TODO: Check that this does not ovewrite ycen
    if ycen is None:
        _ycen = np.arange(npix, dtype=int) if ndim == 1 else np.tile(np.arange(npix), (nap,1)).T
    elif not hasattr(ycen, '__len__'):
        # This is specifically set to be the same size as the input
        # left (not _left)
        _ycen = np.full_like(left, ycen, dtype=int)
    else:
        _ycen = np.asarray(ycen)
    if np.amin(_ycen) < 0 or np.amax(_ycen) > ny-1:
        raise ValueError('Input ycen values will run off the image')
    _ycen = _ycen.astype(int).flatten()

    # Check the sizes match
    if _left.size != _ycen.size:
        raise ValueError('Number of elements in aperture definition (left) and ycen must be equal')

    # TODO: The uses of numpy.newaxis below can create huge arrays.
    # Maybe worth testing if a for loop is faster.

    # Window for the integration for each coordinate
    ix1 = np.floor(_left + 0.5).astype(int)
    ix2 = np.floor(_right + 0.5).astype(int)

    # TODO: The following is slightly different than `recenter_moment`
    # but should nominally be the same. It would also probably be
    # correct if it were instead:
    # fullpix = int(np.amax(np.amin(ix2-ix1)-1,0))+2
    # x = ix1[:,None]+np.arange(fullpix)[None,:]
    # but leaving it for now.
    fullpix = int(np.amax(np.amin(ix2-ix1)-1,0))+4
    x = ix1[:,None]-1+np.arange(fullpix)[None,:]
    ih = np.clip(x,0,nx-1)

    _rad = (_right-_left)/2
    _cen = (_right+_left)/2

    # Set the weight over the window; masked pixels have 0 weight
    good = ((x >= 0) & (x < nx) & np.invert(_mask[_ycen[:,None],ih]) \
                 & (_ivar[_ycen[:,None],ih] > 0)).astype(int)
    wt = good * np.clip(_rad[:,None] - np.abs(x - _cen[:,None]) + 0.5,0,1)

    _flux = flux * _wgt
    _ivar = np.ma.divide(_ivar, np.square(_wgt))

    # Weight the image data
    fwt = _flux[_ycen[:,None],ih] * wt
    # Get the weighted sum
    mu = np.ma.sum(fwt, axis=1)
    # ... and the formal error
    mue = np.ma.sqrt(np.ma.sum(np.ma.divide(np.square(wt), _ivar[_ycen[:,None],ih]), axis=1))

    # Set bad calculations to 0 flux and the error fill value
    # TODO: Include error mask (mue.mask as done below) or ignore
    # problems in the error calculation?
    bad = mu.mask | mue.mask
    mu[bad] = 0
    mue[bad] = fill_error

    # Reshape to the correct size for output if more than one trace was input
    if ndim > 1:
        mu = mu.reshape(npix, nap)
        mue = mue.reshape(npix, nap)
        bad = bad.reshape(npix, nap)

    # Make sure to return unmasked arrays
    return mu.data, mue.data, bad
