""" Module for the SpecObjs and SpecObj classes
"""
from __future__ import absolute_import, division, print_function

import copy
from collections import OrderedDict

import numpy as np

from astropy import units
from astropy.table import Table
from astropy.units import Quantity

from pypeit import msgs
from pypeit.core import parse
from pypeit.core import trace_slits
from pypeit import debugger


class SpecObj(object):
    """Class to handle object spectra from a single exposure
    One generates one of these Objects for each spectrum in the exposure. They are instantiated by the object
    finding routine, and then all spectral extraction information for the object are assigned as attributes

    Parameters:
    ----------
    shape: tuple (nspec, nspat)
       dimensions of the spectral image that the object is identified on
    slit_spat_pos: tuple of floats (spat_left,spat_right)
        The spatial pixel location of the left and right slit trace arrays evaluated at slit_spec_pos (see below). These
        will be in the range (0,nspat)
    slit_spec_pos: float
        The midpoint of the slit location in the spectral direction. This will typically be nspec/2, but must be in the
        range (0,nspec)

    Optional Parameters:
    -------------------
    det:   int
        Detector number. (default = 1, max = 99)
    config: str
       Instrument configuration (default = None)
    scidx: int
       Exposure index (deafult = 1, max=9999)
    objtype: str, optional
       Type of object ('unknown', 'standard', 'science')

    Attributes:
    ----------
    slitcen: float
       Center of slit in fraction of total (trimmed) detector size at ypos
    slitid: int
       Identifier for the slit (max=9999)
    objid: int
       Identifier for the object (max=999)
    """
    # Attributes
    # Init

    # TODO
    def __init__(self, shape, slit_spat_pos, slit_spec_pos, det = 1, config = None,
                 slitid = 999, scidx = 1, objtype='unknown', spat_pixpos=None):

        #Assign from init parameters
        self.shape = shape
        self.slit_spat_pos = slit_spat_pos
        self.slit_spec_pos = slit_spec_pos
        self.config = config
        self.slitid = slitid
        self.scidx = copy.deepcopy(scidx)
        self.det = det
        self.objtype = objtype

        # ToDo add all attributes here and to the documentaiton

        # Object finding attributes
        self.objid = None
        self.idx = None
        self.spat_fracpos = None
        self.smash_peakflux = None
        self.trace_spat = None
        self.trace_spec = None
        self.fwhm = None
        self.spat_pixpos = spat_pixpos # Position on the image in pixels at the midpoint of the slit in spectral direction
        self.maskwidth = None
        self.mincol = None
        self.maxcol = None

        # Attributes for HAND apertures, which are object added to the extraction by hand
        self.HAND_SPEC = None
        self.HAND_SPAT = None
        self.HAND_DET = None
        self.HAND_FWHM = None
        self.HAND_FLAG = False


        # Dictionaries holding boxcar and optimal extraction parameters
        self.boxcar = {}   # Boxcar extraction 'wave', 'counts', 'var', 'sky', 'mask', 'flam', 'flam_var'
        self.optimal = {}  # Optimal extraction 'wave', 'counts', 'var', 'sky', 'mask', 'flam', 'flam_var'


        # Generate IDs
        #self.slitid = int(np.round(self.slitcen*1e4))
        #self.objid = int(np.round(xobj*1e3))

        # Set index
        self.set_idx()

        #

    def set_idx(self):
        # Generate a unique index for this exposure
        #self.idx = '{:02d}'.format(self.setup)
        if self.spat_pixpos is None:
            self.idx = 'O----'
        else:
            self.idx = 'O{:04d}'.format(int(np.rint(self.spat_pixpos)))
        if self.slitid is None:
            self.idx += '-S---'
        else:
            self.idx += '-S{:03d}'.format(self.slitid)
        sdet = parse.get_dnum(self.det, prefix=False)
        self.idx += '-D{:s}'.format(sdet)
        self.idx += '-I{:04d}'.format(self.scidx)

    def check_trace(self, trace, toler=1.):
        """Check that the input trace matches the defined specobjexp

        Parameters:
        ----------
        trace: ndarray
          Trace of the object
        toler: float, optional
          Tolerance for matching, in pixels
        """
        # Trace
        yidx = int(np.round(self.ypos*trace.size))
        obj_trc = trace[yidx]
        # self
        nslit = self.shape[1]*(self.xslit[1]-self.xslit[0])
        xobj_pix = self.shape[1]*self.xslit[0] + nslit*self.xobj
        # Check
        if np.abs(obj_trc-xobj_pix) < toler:
            return True
        else:
            return False

    def copy(self):
        slf = SpecObj(self.shape, self.slit_spat_pos, self.slit_spec_pos, det= self.det,
                      config=self.config, slitid = self.slitid, scidx = self.scidx,
                      objtype=self.objtype, spat_pixpos=self.spat_pixpos)
        slf.boxcar = self.boxcar.copy()
        slf.optimal = self.optimal.copy()
        return slf

    def __getitem__(self, key):
        """ Access the DB groups

        Parameters
        ----------
        key : str or int (or slice)

        Returns
        -------

        """
        # Check
        return getattr(self, key)

    # Printing
#    def __repr__(self):
#        # Generate sets string
#        sdet = parse.get_dnum(self.det, prefix=False)
#        return ('<SpecObj: Setup = {:}, Slit = {:} at spec = {:7.2f} & spat = ({:7.2f},{:7.2f}) on det={:s}, scidx={:}, objid = {:} and objtype={:s}>'.format(
#            self.config, self.slitid, self.slit_spec_pos, self.slit_spat_pos[0], self.slit_spat_pos[1], sdet, self.scidx, self.objid, self.objtype))

    def __repr__(self):
        # Create a single summary table for one object, so that the representation is always the same
        sobjs = SpecObjs(specobjs=[self])
        return sobjs.summary.__repr__()


class SpecObjs(object):
    """
    Object to hold a set of SpecObj objects

    Parameters:
        specobjs : list
        summary : Table
    """

    def __init__(self, specobjs=None):
        """

        Args:
            specobjs: list, optional
        """

        # ToDo Should we just be using numpy object arrays here instead of lists? Seems like that would be easier
        if specobjs is None:
            self.specobjs = []
        else:
            self.specobjs = specobjs

        # Internal summary Table
        self.build_summary()

    def add_sobj(self, sobj):
        """
        Add one or more SpecObj

        The summary table is rebuilt

        Args:
            sobj: SpecObj or list

        Returns:


        """
        if isinstance(sobj, SpecObj):
            self.specobjs += [sobj]
        elif isinstance(sobj, list):
            self.specobjs += sobj
        # Rebuild summary table
        self.build_summary()

    def build_summary(self):
        """

        Returns:
            Builds self.summary Table internally

        """
        if len(self.specobjs) == 0:
            self.summary = Table()
            return
        #
        atts = self.specobjs[0].__dict__.keys()
        uber_dict = {}
        for key in atts:
            uber_dict[key] = []
            for sobj in self.specobjs:
                uber_dict[key] += [getattr(sobj, key)]
        # Build it
        self.summary = Table(uber_dict)

    def remove_sobj(self, index):
        """
        Remove an object

        Args:
            index: int

        Returns:

        """
        self.specobjs.pop(index)
        self.build_summary()

    def __getitem__(self, item):
        """ Overload to allow one to pull an attribute
        or a portion of the SpecObjs list

        Parameters
        ----------
        key : str or int (or slice)

        Returns
        -------

        """
        if isinstance(item, str):
            return self.__getattr__(item)
        elif isinstance(item, (int, np.integer)):
            return self.specobjs[item]
        elif (isinstance(item, slice) or  # Stolen from astropy.table
            isinstance(item, np.ndarray) or
            isinstance(item, list) or
            isinstance(item, tuple) and all(isinstance(x, np.ndarray) for x in item)):
            # here for the many ways to give a slice; a tuple of ndarray
            # is produced by np.where, as in t[np.where(t['a'] > 2)]
            # For all, a new table is constructed with slice of all columns
            sobjs_new = np.array(self.specobjs,dtype=object)
            return SpecObjs(specobjs=sobjs_new[item])


    def __getattr__(self, k):
        """ Generate an array of attribute 'k' from the specobjs

        First attempts to grab data from the Summary table, then the list

        Parameters
        ----------
        k : str
          Attribute

        Returns
        -------
        numpy array
        """
        # JFH I think the summary needs to be rebuilt every time the user tries to slice, since otherwise,
        # newly changed things don't make it into the summary
        self.build_summary()
        # Special case(s)
        if k in self.summary.keys():  # _data
            lst = self.summary[k]
        else:
            lst = None
        # specobjs last!
        if lst is None:
            if len(self.specobjs) == 0:
                raise ValueError("Attribute not available!")
            try:
                lst = [getattr(specobj, k) for specobj in self.specobjs]
            except ValueError:
                raise ValueError("Attribute does not exist")
        # Recast as an array
        return lst_to_array(lst)

    # Printing
    def __repr__(self):
        return self.summary.__repr__()

    def __len__(self):
        return len(self.specobjs)

    def keys(self):
        self.build_summary()
        return self.summary.keys()


def init_exp(lordloc, rordloc, shape, maskslits,
             det, scidx, fitstbl, tracelist, settings, ypos=0.5, **kwargs):
    """ Generate a list of SpecObjExp objects for a given exposure

    Parameters
    ----------
    self
       Instrument "setup" (min=10,max=99)
    scidx : int
       Index of file
    det : int
       Detector index
    tracelist : list of dict
       Contains trace info
    ypos : float, optional [0.5]
       Row on trimmed detector (fractional) to define slit (and object)

    Returns
    -------
    specobjs : list
      List of SpecObjExp objects
    """

    # Init
    specobjs = []
    if fitstbl is None:
        fitsrow = None
    else:
        fitsrow = fitstbl[scidx]
    config = instconfig(fitsrow=fitsrow, binning=settings['detector']['binning'])
    slits = range(len(tracelist))
    gdslits = np.where(~maskslits)[0]

    # Loop on slits
    for sl in slits:
        specobjs.append([])
        # Analyze the slit?
        if sl not in gdslits:
            specobjs[sl].append(None)
            continue
        # Object traces
        if tracelist[sl]['nobj'] != 0:
            # Loop on objects
            #for qq in range(trc_img[sl]['nobj']):
            for qq in range(tracelist[sl]['traces'].shape[1]):
                slitid, slitcen, xslit = trace_slits.get_slitid(shape, lordloc, rordloc,
                                                                 sl, ypos=ypos)
                # xobj
                _, xobj = get_objid(lordloc, rordloc, sl, qq, tracelist, ypos=ypos)
                # Generate
                if tracelist[sl]['object'] is None:
                    specobj = SpecObj((tracelist[0]['object'].shape[:2]), config, scidx, det, xslit, ypos, xobj, **kwargs)
                else:
                    specobj = SpecObj((tracelist[sl]['object'].shape[:2]), config, scidx, det, xslit, ypos, xobj,
                                         **kwargs)
                # Add traces
                specobj.trace = tracelist[sl]['traces'][:, qq]
                # Append
                specobjs[sl].append(copy.deepcopy(specobj))
        else:
            msgs.warn("No objects for slit {0:d}".format(sl+1))
            specobjs[sl].append(None)
    # Return
    return specobjs


def objnm_to_dict(objnm):
    """ Convert an object name or list of them into a dict

    Parameters
    ----------
    objnm : str or list of str

    Returns
    -------
    odict : dict
      Object value or list of object values
    """
    if isinstance(objnm, list):
        tdict = {}
        for kk,iobj in enumerate(objnm):
            idict = objnm_to_dict(iobj)
            if kk == 0:
                for key in idict.keys():
                    tdict[key] = []
            # Fill
            for key in idict.keys():
                tdict[key].append(idict[key])
        # Generate the Table
        return tdict
    # Generate the dict
    prs = objnm.split('-')
    odict = {}
    for iprs in prs:
        odict[iprs[0]] = int(iprs[1:])
    # Return
    return odict


def mtch_obj_to_objects(iobj, objects, stol=50, otol=10, **kwargs):
    """
    Parameters
    ----------
    iobj : str
      Object identifier in format O###-S####-D##
    objects : list
      List of object identifiers
    stol : int
      Tolerance in slit matching
    otol : int
      Tolerance in object matching

    Returns
    -------
    matches : list
      indices of matches in objects
      None if none
    indcies : list

    """
    # Parse input object
    odict = objnm_to_dict(iobj)
    # Generate a Table of the objects
    tbl = Table(objnm_to_dict(objects))

    # Logic on object, slit and detector [ignoring sciidx for now]
    gdrow = (np.abs(tbl['O']-odict['O']) < otol) & (np.abs(tbl['S']-odict['S']) < stol) & (tbl['D'] == odict['D'])
    if np.sum(gdrow) == 0:
        return None
    else:
        return np.array(objects)[gdrow].tolist(), np.where(gdrow)[0].tolist()



def get_objid(lordloc, rordloc, islit, iobj, trc_img, ypos=0.5):
    """ Convert slit position to a slitid
    Parameters
    ----------
    det : int
    islit : int
    iobj : int
    trc_img : list of dict
    ypos : float, optional

    Returns
    -------
    objid : int
    xobj : float
    """
    yidx = int(np.round(ypos*lordloc.shape[0]))
    pixl_slit = lordloc[yidx, islit]
    pixr_slit = rordloc[yidx, islit]
    #
    xobj = (trc_img[islit]['traces'][yidx,iobj]-pixl_slit) / (pixr_slit-pixl_slit)
    objid= int(np.round(xobj*1e3))
    # Return
    return objid, xobj


def instconfig(fitsrow=None, binning=None):
    """ Returns a unique config string

    Parameters
    ----------
    fitsrow : Row
    binnings : str, optional

    Returns
    -------
    config : str
    """

    config_dict = OrderedDict()
    config_dict['S'] = 'slitwid'
    config_dict['D'] = 'dichroic'
    config_dict['G'] = 'dispname'
    config_dict['T'] = 'dispangle'
    #
    config = ''
    for key in config_dict.keys():
        try:
            comp = str(fitsrow[config_dict[key]])
        except (KeyError, TypeError):
            comp = '0'
        #
        val = ''
        for s in comp:
            if s.isdigit():
                val += s
        config = config + key+'{:s}-'.format(val)
    # Binning
    if binning is None:
        msgs.warn("Assuming 1x1 binning for your detector")
        binning = '1x1'
    val = ''
    for s in binning:
        if s.isdigit():
            val = val + s
    config += 'B{:s}'.format(val)
    # Return
    return config


def dummy_specobj(fitstbl, det=1, extraction=True):
    """ Generate dummy specobj classes
    Parameters
    ----------
    fitstbl : Table
      Expecting the fitsdict from dummy_fitsdict
    Returns
    -------

    """
    shape = fitstbl['naxis1'][0], fitstbl['naxis0'][0]
    config = 'AA'
    scidx = 5 # Could be wrong
    xslit = (0.3,0.7) # Center of the detector
    ypos = 0.5
    xobjs = [0.4, 0.6]
    specobjs = []
    for xobj in xobjs:
        specobj = SpecObj(shape, 1240, xslit, spat_pixpos=900, det=det, config=config)
        #specobj = SpecObj(shape, config, scidx, det, xslit, ypos, xobj)
        # Dummy extraction?
        if extraction:
            npix = 2001
            specobj.boxcar['wave'] = np.linspace(4000., 6000., npix)*units.AA
            specobj.boxcar['counts'] = 50.*(specobj.boxcar['wave'].value/5000.)**-1.
            specobj.boxcar['var']  = specobj.boxcar['counts'].copy()
        # Append
        specobjs.append(specobj)
    # Return
    return specobjs

#TODO We need a method to write these objects to a fits file

def lst_to_array(lst, mask=None):
    """ Simple method to convert a list to an array

    Allows for a list of Quantity objects

    Parameters
    ----------
    lst : list
      Should be number or Quantities
    mask : boolean array, optional

    Returns
    -------
    array or Quantity array

    """
    if mask is None:
        mask = np.array([True]*len(lst))
    if isinstance(lst[0], Quantity):
        return Quantity(lst)[mask]
    else:
        return np.array(lst)[mask]
        # Generate the Table
        tbl = Table(clms, names=attrib)
        # Return
        return tbl