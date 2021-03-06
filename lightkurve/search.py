"""Defines tools to retrieve Kepler data from the archive at MAST."""
from __future__ import division
import os
import glob
import logging
import re
import warnings
from requests import HTTPError

import numpy as np
from astropy.table import join, Table, Row
from astropy.coordinates import SkyCoord
from astropy.io import ascii
from astropy import units as u
from astropy.utils import deprecated

from .targetpixelfile import TargetPixelFile
from .collections import TargetPixelFileCollection, LightCurveCollection
from .utils import suppress_stdout, LightkurveWarning, LightkurveDeprecationWarning
from .io import read
from . import PACKAGEDIR

log = logging.getLogger(__name__)

__all__ = ['search_targetpixelfile', 'search_lightcurve',
           'search_lightcurvefile', 'search_tesscut',
           'SearchResult']


class SearchError(Exception):
    pass


class SearchResult(object):
    """Container for the results returned by `search_targetpixelfile`,
    `search_lightcurve`, or `search_tesscut`.

    The purpose of this class is to provide a convenient way to inspect and
    download products that have been identified using one of the data search
    functions.

    Parameters
    ----------
    table : `astropy.table.Table` object
        Astropy table returned by a join of the astroquery `Observations.query_criteria()`
        and `Observations.get_product_list()` methods.
    """
    def __init__(self, table=None):
        if table is None:
            self.table = Table()
        else:
            self.table = table
            if len(table) > 0:
                self._add_columns()

    def _add_columns(self):
        """Adds user-friendly index (``#``) column.

        These columns are not part of the MAST Portal API, but they make the
        display of search results much nicer in Lightkurve.
        """
        self.table['#'] = None
        for idx in range(len(self.table)):
            self.table['#'][idx] = idx

    def __repr__(self, html=False):
        out = 'SearchResult containing {} data products.'.format(len(self.table))
        if len(self.table) == 0:
            return out
        columns = ['#', 'observation', 'author', 'target_name', 'productFilename', 'distance']
        return out + '\n\n' + '\n'.join(self.table[columns].pformat(max_width=300, html=html))

    def _repr_html_(self):
        return self.__repr__(html=True)

    def __getitem__(self, key):
        """Implements indexing and slicing, e.g. SearchResult[2:5]."""
        # this check is necessary due to an astropy bug
        # for more information, see issue #445
        if key == -1:
            key = len(self.table) - 1
        selection = self.table[key]
        # Indexing a Table with an integer will return a Row
        if isinstance(selection, Row):
            selection = Table(selection)
        return SearchResult(table=selection)

    def __len__(self):
        """Returns the number of products in the SearchResult table."""
        return len(self.table)

    @property
    def unique_targets(self):
        """Returns a table of targets and their RA & dec values produced by search"""
        mask = ['target_name', 's_ra', 's_dec']
        return Table.from_pandas(self.table[mask].to_pandas().drop_duplicates('target_name').reset_index(drop=True))

    @property
    def obsid(self):
        """Returns an array of MAST observation IDs"""
        return np.asarray(np.unique(self.table['obsid']), dtype='int64')

    @property
    def target_name(self):
        """Returns an array of target names"""
        return self.table['target_name'].data.data

    @property
    def ra(self):
        """Returns an array of RA values for targets in search"""
        return self.table['s_ra'].data.data

    @property
    def dec(self):
        """Returns an array of dec values for targets in search"""
        return self.table['s_dec'].data.data

    def _download_one(self, table, quality_bitmask, download_dir, cutout_size, **kwargs):
        """Private method used by `download()` and `download_all()` to download
        exactly one file from the MAST archive.

        Always returns a `TargetPixelFile` or `LightCurve` object.
        """
        # Make sure astroquery uses the same level of verbosity
        logging.getLogger('astropy').setLevel(log.getEffectiveLevel())

        if download_dir is None:
            download_dir = self._default_download_dir()

        # if the SearchResult row is a TESScut entry, then download cutout
        if 'FFI Cutout' in table[0]['description']:
            try:
                log.debug("Started downloading TESSCut for '{}' sector {}."
                          "".format(table[0]['target_name'], table[0]['sequence_number']))
                path = self._fetch_tesscut_path(table[0]['target_name'],
                                                table[0]['sequence_number'],
                                                download_dir,
                                                cutout_size)
            except Exception as exc:
                msg = str(exc)
                if "504" in msg:
                    # TESSCut will occasionally return a "504 Gateway Timeout
                    # error" when it is overloaded.
                    raise HTTPError('The TESS FFI cutout service at MAST appears '
                                    'to be temporarily unavailable. It returned '
                                    'the following error: {}'.format(exc))
                else:
                    raise SearchError('Unable to download FFI cutout. Desired target '
                                    'coordinates may be too near the edge of the FFI.'
                                    'Error: {}'.format(exc))

            return read(path,
                        quality_bitmask=quality_bitmask,
                        targetid=table[0]['targetid'])

        else:
            if cutout_size is not None:
                warnings.warn('`cutout_size` can only be specified for TESS '
                              'Full Frame Image cutouts.', LightkurveWarning)
            from astroquery.mast import Observations
            log.debug("Started downloading {}.".format(table[:1]['dataURL'][0]))
            path = Observations.download_products(table[:1], mrp_only=False,
                                                  download_dir=download_dir)['Local Path'][0]
            log.debug("Finished downloading.")
            return read(path, quality_bitmask=quality_bitmask, **kwargs)

    @suppress_stdout
    def download(self, quality_bitmask='default', download_dir=None, cutout_size=None, **kwargs):
        """Returns a single `LightCurve` or `TargetPixelFile` object.

        If multiple files are present in `SearchResult.table`, only the first
        will be downloaded.

        Parameters
        ----------
        quality_bitmask : str or int
            Bitmask (integer) which identifies the quality flag bitmask that should
            be used to mask out bad cadences. If a string is passed, it has the
            following meaning:

                * "none": no cadences will be ignored (`quality_bitmask=0`).
                * "default": cadences with severe quality issues will be ignored
                  (`quality_bitmask=1130799`).
                * "hard": more conservative choice of flags to ignore
                  (`quality_bitmask=1664431`). This is known to remove good data.
                * "hardest": removes all data that has been flagged
                  (`quality_bitmask=2096639`). This mask is not recommended.

            See the :class:`KeplerQualityFlags` class for details on the bitmasks.
        download_dir : str
            Location where the data files will be stored.
            Defaults to "~/.lightkurve-cache" if `None` is passed.
        cutout_size : int, float or tuple
            Side length of cutout in pixels. Tuples should have dimensions (y, x).
            Default size is (5, 5)
        kwargs : dict
            Extra keyword arguments passed on to the file format reader function.

        Returns
        -------
        data : `TargetPixelFile` or `LightCurve` object
            The first entry in the products table.

        Raises
        ------
        HTTPError
            If the TESSCut service times out (i.e. returns HTTP status 504).
        SearchError
            If any other error occurs.
        """
        if len(self.table) == 0:
            warnings.warn("Cannot download from an empty search result.",
                          LightkurveWarning)
            return None
        if len(self.table) != 1:
            warnings.warn('Warning: {} files available to download. '
                          'Only the first file has been downloaded. '
                          'Please use `download_all()` or specify additional '
                          'criteria (e.g. quarter, campaign, or sector) '
                          'to limit your search.'.format(len(self.table)),
                          LightkurveWarning)

        return self._download_one(table=self.table[:1],
                                  quality_bitmask=quality_bitmask,
                                  download_dir=download_dir,
                                  cutout_size=cutout_size,
                                  **kwargs)

    @suppress_stdout
    def download_all(self, quality_bitmask='default', download_dir=None, cutout_size=None, **kwargs):
        """Returns a `~lightkurve.collections.TargetPixelFileCollection` or
        `~lightkurve.collections.LightCurveCollection`.

         Parameters
         ----------
         quality_bitmask : str or int
             Bitmask (integer) which identifies the quality flag bitmask that should
             be used to mask out bad cadences. If a string is passed, it has the
             following meaning:

                 * "none": no cadences will be ignored (`quality_bitmask=0`).
                 * "default": cadences with severe quality issues will be ignored
                   (`quality_bitmask=1130799`).
                 * "hard": more conservative choice of flags to ignore
                   (`quality_bitmask=1664431`). This is known to remove good data.
                 * "hardest": removes all data that has been flagged
                   (`quality_bitmask=2096639`). This mask is not recommended.

             See the :class:`KeplerQualityFlags` class for details on the bitmasks.
        download_dir : str
            Location where the data files will be stored.
            Defaults to "~/.lightkurve-cache" if `None` is passed.
        cutout_size : int, float or tuple
            Side length of cutout in pixels. Tuples should have dimensions (y, x).
            Default size is (5, 5)
        kwargs : dict
            Extra keyword arguments passed on to the file format reader function.

        Returns
        -------
        collection : `~lightkurve.collections.Collection` object
            Returns a `~lightkurve.collections.LightCurveCollection` or
            `~lightkurve.collections.TargetPixelFileCollection`,
            containing all entries in the products table

        Raises
        ------
        HTTPError
            If the TESSCut service times out (i.e. returns HTTP status 504).
        SearchError
            If any other error occurs.
        """
        if len(self.table) == 0:
            warnings.warn("Cannot download from an empty search result.",
                          LightkurveWarning)
            return None
        log.debug("{} files will be downloaded.".format(len(self.table)))

        products = []
        for idx in range(len(self.table)):
            products.append(self._download_one(table=self.table[idx:idx+1],
                                               quality_bitmask=quality_bitmask,
                                               download_dir=download_dir,
                                               cutout_size=cutout_size,
                                               **kwargs))
        if isinstance(products[0], TargetPixelFile):
            return TargetPixelFileCollection(products)
        else:
            return LightCurveCollection(products)

    def _default_download_dir(self):
        """Returns the default path to the directory where files will be downloaded.

        By default, this method will return "~/.lightkurve-cache" and create
        this directory if it does not exist.  If the directory cannot be
        access or created, then it returns the local directory (".").

        Returns
        -------
        download_dir : str
            Path to location of `mastDownload` folder where data downloaded from MAST are stored
        """
        download_dir = os.path.join(os.path.expanduser('~'), '.lightkurve-cache')
        if os.path.isdir(download_dir):
            return download_dir
        else:
            # if it doesn't exist, make a new cache directory
            try:
                os.mkdir(download_dir)
            # downloads locally if OS error occurs
            except OSError:
                log.warning('Warning: unable to create {}. '
                            'Downloading MAST files to the current '
                            'working directory instead.'.format(download_dir))
                download_dir = '.'

        return download_dir

    def _fetch_tesscut_path(self, target, sector, download_dir, cutout_size):
        """Downloads TESS FFI cutout and returns path to local file.

        Parameters
        ----------
        download_dir : str
            Path to location of `.lightkurve-cache` directory where downloaded
            cutouts are stored
        cutout_size : int, float or tuple
            Side length of cutout in pixels. Tuples should have dimensions (y, x).
            Default size is (5, 5)

        Returns
        -------
        path : str
            Path to locally downloaded cutout file
        """
        from astroquery.mast import TesscutClass
        coords = _resolve_object(target)

        # Set cutout_size defaults
        if cutout_size is None:
            cutout_size = 5

        # Check existence of `~/.lightkurve-cache/tesscut`
        tesscut_dir = os.path.join(download_dir, 'tesscut')
        if not os.path.isdir(tesscut_dir):
            # if it doesn't exist, make a new cache directory
            try:
                os.mkdir(tesscut_dir)
            # downloads into default cache if OSError occurs
            except OSError:
                tesscut_dir = download_dir

        # Resolve SkyCoord of given target
        coords = _resolve_object(target)

        # build path string name and check if it exists
        # this is necessary to ensure cutouts are not downloaded multiple times
        sec = TesscutClass().get_sectors(coords)
        sector_name = sec[sec['sector'] == sector]['sectorName'][0]
        if isinstance(cutout_size, int):
            size_str = str(int(cutout_size)) + 'x' + str(int(cutout_size))
        elif isinstance(cutout_size, tuple) or isinstance(cutout_size, list):
            size_str = str(int(cutout_size[1])) + 'x' + str(int(cutout_size[0]))

        # search cache for file with matching ra, dec, and cutout size
        # ra and dec are searched within 0.001 degrees of input target
        ra_string = str(coords.ra.value)
        dec_string = str(coords.dec.value)
        matchstring = r"{}_{}*_{}*_{}_astrocut.fits".format(sector_name,
                                                            ra_string[:ra_string.find('.')+4],
                                                            dec_string[:dec_string.find('.')+4],
                                                            size_str)
        cached_files = glob.glob(os.path.join(tesscut_dir, matchstring))

        # if any files exist, return the path to them instead of downloading
        if len(cached_files) > 0:
            path = cached_files[0]
            log.debug("Cached file found.")
        # otherwise the file will be downloaded
        else:
            cutout_path = TesscutClass().download_cutouts(coords, size=cutout_size,
                                                          sector=sector, path=tesscut_dir)
            path = os.path.join(download_dir, cutout_path[0][0])
            log.debug("Finished downloading.")
        return path


def search_targetpixelfile(target, radius=None, cadence='long',
                           mission=('Kepler', 'K2', 'TESS'),
                           author=('Kepler', 'K2', 'SPOC'),
                           quarter=None, month=None, campaign=None, sector=None,
                           limit=None):
    """Searches the `public data archive at MAST <https://archive.stsci.edu>`_
    for target pixel files.

    This function fetches a data table that lists the Target Pixel Files (TPFs)
    that fall within a region of sky centered around the position of `target`
    and within a cone of a given `radius`. If no value is provided for `radius`,
    only a single target will be returned.

    Parameters
    ----------
    target : str, int, or `astropy.coordinates.SkyCoord` object
        Target around which to search. Valid inputs include:

            * The name of the object as a string, e.g. "Kepler-10".
            * The KIC or EPIC identifier as an integer, e.g. 11904151.
            * A coordinate string in decimal format, e.g. "285.67942179 +50.24130576".
            * A coordinate string in sexagesimal format, e.g. "19:02:43.1 +50:14:28.7".
            * An `astropy.coordinates.SkyCoord` object.
    radius : float or `astropy.units.Quantity` object
        Conesearch radius.  If a float is given it will be assumed to be in
        units of arcseconds.  If `None` then we default to 0.0001 arcsec.
    cadence : str
        'long' or 'short'.
    mission : str, tuple of str
        'Kepler', 'K2', or 'TESS'. By default, all will be returned.
    author : str, tuple of str, or "any"
        Author of the data product (`provenance_name` in the MAST API).
        Defaults to the official products: ('Kepler', 'K2', 'SPOC').
        Use "any" to retrieve all light curves regardless of the author.
    quarter, campaign, sector : int, list of ints
        Kepler Quarter, K2 Campaign, or TESS Sector number.
        By default all quarters/campaigns/sectors will be returned.
    month : 1, 2, 3, 4 or list of int
        For Kepler's prime mission, there are three short-cadence
        TargetPixelFiles for each quarter, each covering one month.
        Hence, if cadence='short' you can specify month=1, 2, 3, or 4.
        By default all months will be returned.
    limit : int
        Maximum number of products to return.

    Returns
    -------
    result : :class:`SearchResult` object
        Object detailing the data products found.

    Examples
    --------
    This example demonstrates how to use the `search_targetpixelfile()` function
    to query and download data. Before instantiating a
    `~lightkurve.targetpixelfile.KeplerTargetPixelFile` object or
    downloading any science products, we can identify potential desired targets
    with `search_targetpixelfile()`::

        >>> search_result = search_targetpixelfile('Kepler-10')  # doctest: +SKIP
        >>> print(search_result)  # doctest: +SKIP

    The above code will query mast for Target Pixel Files (TPFs) available for
    the known planet system Kepler-10, and display a table containing the
    available science products. Because Kepler-10 was observed during 15 Quarters,
    the table will have 15 entries. To obtain a
    `~lightkurve.collections.TargetPixelFileCollection` object containing all
    15 observations, use::

        >>> search_result.download_all()  # doctest: +SKIP

    or we can download a single product by limiting our search::

        >>> tpf = search_targetpixelfile('Kepler-10', quarter=2).download()  # doctest: +SKIP

    The above line of code will only download Quarter 2 and create a single
    `~lightkurve.targetpixelfile.KeplerTargetPixelFile` object called `tpf`.

    We can also pass a radius into `search_targetpixelfile` to perform a cone search::

        >>> search_targetpixelfile('Kepler-10', radius=100).targets  # doctest: +SKIP

    This will display a table containing all targets within 100 arcseconds of Kepler-10.
    We can download a `~lightkurve.collections.TargetPixelFileCollection` object
    containing all available products for these targets in Quarter 4 with::

        >>> search_targetpixelfile('Kepler-10', radius=100, quarter=4).download_all()  # doctest: +SKIP
    """
    try:
        return _search_products(target, radius=radius, filetype="Target Pixel",
                                cadence=cadence, mission=mission,
                                provenance_name=author,
                                quarter=quarter, month=month,
                                campaign=campaign, sector=sector,
                                limit=limit)
    except SearchError as exc:
        log.error(exc)
        return SearchResult(None)


@deprecated("2.0", alternative="search_lightcurve()", warning_type=LightkurveDeprecationWarning)
def search_lightcurvefile(*args, **kwargs):
    return search_lightcurve(*args, **kwargs)


def search_lightcurve(target, radius=None, cadence='long',
                      mission=('Kepler', 'K2', 'TESS'),
                      author=('Kepler', 'K2', 'SPOC'),
                      quarter=None, month=None, campaign=None, sector=None,
                      limit=None):
    """Searches the `public data archive at MAST <https://archive.stsci.edu>`_ for a Kepler or TESS
    :class:`LightCurve <lightkurve.lightcurve.LightCurve>`.

    This function fetches a data table that lists the Light Curve Files
    that fall within a region of sky centered around the position of `target`
    and within a cone of a given `radius`. If no value is provided for `radius`,
    only a single target will be returned.

    Parameters
    ----------
    target : str, int, or `astropy.coordinates.SkyCoord` object
        Target around which to search. Valid inputs include:

            * The name of the object as a string, e.g. "Kepler-10".
            * The KIC or EPIC identifier as an integer, e.g. 11904151.
            * A coordinate string in decimal format, e.g. "285.67942179 +50.24130576".
            * A coordinate string in sexagesimal format, e.g. "19:02:43.1 +50:14:28.7".
            * An `astropy.coordinates.SkyCoord` object.
    radius : float or `astropy.units.Quantity` object
        Conesearch radius.  If a float is given it will be assumed to be in
        units of arcseconds.  If `None` then we default to 0.0001 arcsec.
    cadence : str
        'long' or 'short'.
    mission : str, tuple of str
        'Kepler', 'K2', or 'TESS'. By default, all will be returned.
    author : str, tuple of str, or "any"
        Author of the data product (`provenance_name` in the MAST API).
        Defaults to the official products: ('Kepler', 'K2', 'SPOC').
        Community-provided products that are supported include ('K2SFF', 'EVEREST').
        Use "any" to retrieve all light curves regardless of the author.
    quarter, campaign, sector : int, list of ints
        Kepler Quarter, K2 Campaign, or TESS Sector number.
        By default all quarters/campaigns/sectors will be returned.
    month : 1, 2, 3, 4 or list of int
        For Kepler's prime mission, there are three short-cadence
        TargetPixelFiles for each quarter, each covering one month.
        Hence, if cadence='short' you can specify month=1, 2, 3, or 4.
        By default all months will be returned.
    limit : int
        Maximum number of products to return.

    Returns
    -------
    result : :class:`SearchResult` object
        Object detailing the data products found.

    Examples
    --------
    This example demonstrates how to use the `search_lightcurve()` function to
    query and download data. Before instantiating a `KeplerLightCurve` object or
    downloading any science products, we can identify potential desired targets with
    `search_lightcurve`::

        >>> from lightkurve import search_lightcurvefile  # doctest: +SKIP
        >>> search_result = search_lightcurvefile("Kepler-10")  # doctest: +SKIP
        >>> print(search_result)  # doctest: +SKIP

    The above code will query mast for lightcurve files available for the known
    planet system Kepler-10, and display a table containing the available
    data products. Because Kepler-10 was observed in 15 quarters, the search
    result will list 15 different files. If we want to download a
    `~lightkurve.collections.LightCurveFileCollection` object containing all
    15 observations, use::

        >>> search_result.download_all()  # doctest: +SKIP

    or we can specify the downloaded products by limiting our search::

        >>> lcf = search_lightcurvefile('Kepler-10', quarter=2).download()  # doctest: +SKIP

    The above line of code will only search and download Quarter 2 data and
    create a `LightCurveFile` object called lcf.

    We can also pass a radius into `search_lightcurvefile` to perform a cone search::

        >>> search_lightcurvefile('Kepler-10', radius=100, quarter=4)  # doctest: +SKIP

    This will display a table containing all targets within 100 arcseconds of
    Kepler-10 and in Quarter 4.  We can then download a
    `~lightkurve.collections.LightCurveFileCollection` containing all these
    products using::

        >>> search_lightcurvefile('kepler-10', radius=100, quarter=4).download_all()  # doctest: +SKIP
    """
    try:
        return _search_products(target, radius=radius, filetype="Lightcurve",
                                cadence=cadence, mission=mission,
                                provenance_name=author,
                                quarter=quarter, month=month,
                                campaign=campaign, sector=sector, limit=limit)
    except SearchError as exc:
        log.error(exc)
        return SearchResult(None)


def search_tesscut(target, sector=None):
    """Searches MAST for TESS Full Frame Image cutouts containing a desired target or region.

    This feature uses the `TESScut service <https://mast.stsci.edu/tesscut/>`_
    provided by the TESS data archive at MAST.  If you use this service in
    your work, please `cite TESScut <https://ascl.net/code/v/2239>`_ in your
    publications.

    Parameters
    ----------
    target : str, int, or `astropy.coordinates.SkyCoord` object
        Target around which to search. Valid inputs include:

            * The name of the object as a string, e.g. "Kepler-10".
            * The KIC or EPIC identifier as an integer, e.g. 11904151.
            * A coordinate string in decimal format, e.g. "285.67942179 +50.24130576".
            * A coordinate string in sexagesimal format, e.g. "19:02:43.1 +50:14:28.7".
            * An `astropy.coordinates.SkyCoord` object.
    sector : int or list
        TESS Sector number. Default (None) will return all available sectors. A
        list of desired sectors can also be provided.

    Returns
    -------
    result : :class:`SearchResult` object
        Object detailing the data products found.
    """
    try:
        return _search_products(target, filetype="ffi", mission='TESS', sector=sector)
    except SearchError as exc:
        log.error(exc)
        return SearchResult(None)


def _search_products(target, radius=None, filetype="Lightcurve", cadence='long',
                     mission=('Kepler', 'K2', 'TESS'),
                     provenance_name=('Kepler', 'K2', 'SPOC'),
                     t_exptime=(0, 9999), quarter=None, month=None,
                     campaign=None, sector=None, limit=None,
                     **extra_query_criteria):
    """Helper function which returns a SearchResult object containing MAST
    products that match several criteria.

    Parameters
    ----------
    target : str, int, or `astropy.coordinates.SkyCoord` object
        See docstrings above.
    radius : float or `astropy.units.Quantity` object
        Conesearch radius.  If a float is given it will be assumed to be in
        units of arcseconds.  If `None` then we default to 0.0001 arcsec.
    filetype : {'Target pixel', 'Lightcurve', 'FFI'}
        Type of files queried at MAST.
    cadence : str
        Desired cadence (`long`, `short`, `any`)
    mission : str, list of str
        'Kepler', 'K2', or 'TESS'. By default, all will be returned.
    provenance_name : str, list of str
        Provenance of the data product. Defaults to official products, i.e.
        ('Kepler', 'K2', 'SPOC').  Community-provided products such as 'K2SFF'
        are supported as well.
    quarter, campaign, sector : int, list of ints
        Kepler Quarter, K2 Campaign, or TESS Sector number.
        By default all quarters/campaigns/sectors will be returned.
    month : 1, 2, 3, 4 or list of int
        For Kepler's prime mission, there are three short-cadence
        TargetPixelFiles for each quarter, each covering one month.
        Hence, if cadence='short' you can specify month=1, 2, 3, or 4.
        By default all months will be returned.
    limit : int
        Maximum number of products to return

    Returns
    -------
    SearchResult : :class:`SearchResult` object.
    """
    if isinstance(target, int):
        if (0 < target) and (target < 13161030):
            log.warning("Warning: {} may refer to a different Kepler or TESS target. "
                        "Please add the prefix 'KIC' or 'TIC' to disambiguate."
                        "".format(target))
        elif (0 < 200000000) and (target < 251813739):
            log.warning("Warning: {} may refer to a different K2 or TESS target. "
                        "Please add the prefix 'EPIC' or 'TIC' to disambiguate."
                        "".format(target))

    # Ensure mission is a list
    mission = np.atleast_1d(mission).tolist()

    # Avoid filtering on `provenance_name` if `author` equals "any" or "all"
    if provenance_name in ("any", "all") or provenance_name is None:
        provenance_name = None
    else:
        provenance_name = np.atleast_1d(provenance_name).tolist()

    # Speed up by restricting the MAST query if we don't want FFI image data
    extra_query_criteria = {}
    if filetype in ['Lightcurve', 'Target Pixel']:
        # At MAST, non-FFI Kepler pipeline products are known as "cube" products,
        # and non-FFI TESS pipeline products are listed as "timeseries".
        extra_query_criteria['dataproduct_type'] = ['cube', 'timeseries']
    # Make sure `search_tesscut` always performs a cone search (i.e. always
    # passed a radius value), because strict target name search does not apply.
    if filetype.lower() == 'ffi' and radius is None:
        radius = .0001 * u.arcsec
    observations = _query_mast(target, radius=radius,
                               project=mission,
                               provenance_name=provenance_name,
                               t_exptime=t_exptime,
                               sequence_number=campaign or sector,
                               **extra_query_criteria)
    log.debug("MAST found {} observations. "
              "Now querying MAST for the corresponding data products."
              "".format(len(observations)))
    if len(observations) == 0:
        raise SearchError('No data found for target "{}".'.format(target))

    # Light curves and target pixel files
    if filetype.lower() != 'ffi':
        from astroquery.mast import Observations
        products = Observations.get_product_list(observations)
        result = join(observations, products, keys="obs_id", join_type='right',
                      uniq_col_name='{col_name}{table_name}', table_names=['', '_products'])
        result.sort(['distance', 'obs_id'])

        # Add the user-friendly 'author' column (synonym for 'provenance_name')
        result['author'] = result['provenance_name']
        # Add the user-friendly 'observation' column
        result['observation'] = None
        obs_prefix = {'Kepler': 'Quarter', 'K2': 'Campaign', 'TESS': 'Sector'}
        for idx in range(len(result)):
            obs_project = result['project'][idx]
            obs_seqno = result['sequence_number'][idx]
            # Kepler sequence_number values were not populated at the time of
            # writing this code, so we parse them from the description field.
            if obs_project == 'Kepler' and result['sequence_number'].mask[idx]:
                try:
                    obs_seqno = re.findall(r".*Q(\d+)", result['description'][idx])[0]
                except IndexError:
                    obs_seqno = ""
            result['observation'][idx] = "{} {} {}".format(obs_project,
                                                           obs_prefix.get(obs_project, ""),
                                                           obs_seqno)

        masked_result = _filter_products(result, filetype=filetype,
                                         campaign=campaign, quarter=quarter,
                                         cadence=cadence, project=mission,
                                         provenance_name=provenance_name,
                                         month=month, sector=sector, limit=limit)
        log.debug("MAST found {} matching data products.".format(len(masked_result)))
        masked_result['distance'].info.format = '.1f'  # display <0.1 arcsec
        return SearchResult(masked_result)

    # Full Frame Images
    else:
        cutouts = []
        for idx in np.where(['TESS FFI' in t for t in observations['target_name']])[0]:
            # if target passed in is a SkyCoord object, convert to RA, dec pair
            if isinstance(target, SkyCoord):
                target = '{}, {}'.format(target.ra.deg, target.dec.deg)
            # pull sector numbers
            s = observations['sequence_number'][idx]
            # if the desired sector is available, add a row
            if s in np.atleast_1d(sector) or sector is None:
                cutouts.append({'description': f'TESS FFI Cutout (sector {s})',
                                'observation': f'TESS Sector {s}',
                                'target_name': str(target),
                                'targetid': str(target),
                                'productFilename': 'TESSCut',
                                'provenance_name': 'MAST',
                                'author': 'MAST',
                                'distance': 0.0,
                                'sequence_number': s,
                                'project': 'TESS',
                                'obs_collection': 'TESS'}
                               )
        if len(cutouts) > 0:
            log.debug("Found {} matching cutouts.".format(len(cutouts)))
            masked_result = Table(cutouts)
            masked_result.sort(['distance', 'sequence_number'])
        else:
            masked_result = None
        return SearchResult(masked_result)


def _query_mast(target, radius=None,
                project=('Kepler', 'K2', 'TESS'),
                provenance_name=("Kepler", "K2", "SPOC"),
                t_exptime=(0, 9999),
                sequence_number=None,
                **extra_query_criteria):
    """Helper function which wraps `astroquery.mast.Observations.query_criteria()`
    to return a table of all Kepler/K2/TESS observations of a given target.

    By default only the official data products are returned, but this can be
    adjusted by adding alternative data product names into `provenance_name`.

    Parameters
    ----------
    target : str, int, or `astropy.coordinates.SkyCoord` object
        See docstrings above.
    radius : float or `astropy.units.Quantity` object
        Conesearch radius.  If a float is given it will be assumed to be in
        units of arcseconds.  If `None` then we default to 0.0001 arcsec.
    project : str, list of str
        Mission name.  Typically 'Kepler', 'K2', or 'TESS'.
        This parameter is case-insensitive.
    provenance_name : str, list of str
        Provenance of the observation.  Common options include 'Kepler', 'K2',
        'SPOC', 'K2SFF', 'EVEREST', 'KEPSEISMIC'.
        This parameter is case-insensitive.
    t_exptime : (float, float) tuple
        Exposure time range in seconds. Common values include `(59, 61)`
        for Kepler short cadence and `(1799, 1801)` for Kepler long cadence.
    sequence_number : int, list of int
        Quarter, Campaign, or Sector number.
    **extra_query_criteria : kwargs
        Extra criteria to be passed to `astroquery.mast.Observations.query_criteria`.

    Returns
    -------
    obs : astropy.Table
        Table detailing the available observations on MAST.
    """
    # Local astroquery import because the package is not used elsewhere
    from astroquery.mast import Observations
    from astroquery.exceptions import ResolverError, NoResultsWarning

    # If passed a SkyCoord, convert it to an "ra, dec" string for MAST
    if isinstance(target, SkyCoord):
        target = '{}, {}'.format(target.ra.deg, target.dec.deg)

    # We pass the following `query_criteria` to MAST regardless of whether
    # we search by position or target name:
    query_criteria = {
        'project': project,
        **extra_query_criteria
        }
    if provenance_name is not None:
        query_criteria['provenance_name'] = provenance_name
    if sequence_number is not None:
        query_criteria['sequence_number'] = sequence_number
    if t_exptime is not None:
        query_criteria['t_exptime'] = t_exptime

    # If an exact KIC ID is passed, we will search by the exact `target_name`
    # under which MAST will know the object to prevent source confusion.
    # For discussion, see e.g. GitHub issues #148, #718.
    exact_target_name = None
    target_lower = str(target).lower()
    # Was a Kepler target ID passed?
    kplr_match = re.match("^(kplr|kic) ?(\d+)$", target_lower)
    if kplr_match:
        exact_target_name = f"kplr{kplr_match.group(2).zfill(9)}"
    # Was a K2 target ID passed?
    ktwo_match = re.match("^(ktwo|epic) ?(\d+)$", target_lower)
    if ktwo_match:
        exact_target_name = f"ktwo{ktwo_match.group(2).zfill(9)}"
    # Was a TESS target ID passed?
    tess_match = re.match("^(tess|tic) ?(\d+)$", target_lower)
    if tess_match:
        exact_target_name = f"{tess_match.group(2).zfill(9)}"

    if exact_target_name and radius is None:
        log.debug("Started querying MAST for observations with the exact "
                  f"target_name='{exact_target_name}'.")
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", category=NoResultsWarning)
            obs = Observations.query_criteria(target_name=exact_target_name,
                                              **query_criteria)
        if len(obs) > 0:
            # astroquery does not report distance when querying by `target_name`;
            # we add it here so that the table returned always has this column.
            obs['distance'] = 0.
            return obs
        else:
            log.debug(f"No observations found. Now performing a cone search instead.")

    # If the above did not return a result, then do a cone search using the MAST name resolver
    # `radius` defaults to 0.0001 and unit arcsecond
    if radius is None:
        radius = .0001 * u.arcsec
    elif not isinstance(radius, u.quantity.Quantity):
        radius = radius * u.arcsec
    query_criteria['radius'] = str(radius.to(u.deg))

    try:
        log.debug("Started querying MAST for observations within "
                  f"{radius.to(u.arcsec)} arcsec of objectname='{target}'.")
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", category=NoResultsWarning)
            obs = Observations.query_criteria(objectname=target,
                                              **query_criteria)
        obs.sort('distance')
        return obs
    except ResolverError as exc:
        # MAST failed to resolve the object name to sky coordinates
        raise SearchError(exc) from exc


def _filter_products(products, campaign=None, quarter=None, month=None,
                     sector=None, cadence='long', limit=None,
                     project=('Kepler', 'K2', 'TESS'),
                     provenance_name=('Kepler', 'K2', 'SPOC'),
                     filetype='Target Pixel'):
    """Helper function which filters a SearchResult's products table by one or
    more criteria.

    Parameters
    ----------
    products : `astropy.table.Table` object
        Astropy table containing data products returned by MAST
    campaign : int or list
        Desired campaign of observation for data products
    quarter : int or list
        Desired quarter of observation for data products
    month : int or list
        Desired month of observation for data products
    cadence : str
        Desired cadence (`long`, `short`, `any`)
    filetype : str
        Type of files queried at MAST (`Target Pixel` or `Lightcurve`).

    Returns
    -------
    products : `astropy.table.Table` object
        Masked astropy table containing desired data products
    """
    if provenance_name is None:  # apply all filters
        provenance_lower = ('kepler', 'k2', 'spoc')
    else:
        provenance_lower = [p.lower() for p in np.atleast_1d(provenance_name)]

    mask = np.ones(len(products), dtype=bool)

    # Kepler data needs a special filter for quarter, month, and file type
    mask &= ~np.array([prov.lower() == 'kepler' for prov in products['provenance_name']])
    if 'kepler' in provenance_lower and campaign is None and sector is None:
        mask |= _mask_kepler_products(products, quarter=quarter, month=month,
                                      cadence=cadence, filetype=filetype)

    # K2 data needs a special filter for file type
    mask &= ~np.array([prov.lower() == 'k2' for prov in products['provenance_name']])
    if 'k2' in provenance_lower and quarter is None and sector is None:
        mask |= _mask_k2_products(products, campaign=campaign,
                                  cadence=cadence, filetype=filetype)

    # TESS SPOC data needs a special filter for file type
    mask &= ~np.array([prov.lower() == 'spoc' for prov in products['provenance_name']])
    if 'spoc' in provenance_lower and quarter is None and campaign is None:
        mask |= _mask_spoc_products(products, sector=sector, filetype=filetype)

    # Allow only fits files
    mask &= np.array([uri.lower().endswith('fits') or
                      uri.lower().endswith('fits.gz')
                      for uri in products['productFilename']])

    products = products[mask]

    products.sort(['distance', 'productFilename'])
    if limit is not None:
        return products[0:limit]
    return products


def _mask_kepler_products(products, quarter=None, month=None, cadence='long',
                          filetype='Target Pixel'):
    """Returns a mask flagging the Kepler products that match the criteria."""
    mask = np.array([proj.lower() == 'kepler' for proj in products['provenance_name']])
    if mask.sum() == 0:
        return mask

    # Filters on cadence and product type
    if cadence in ['short', 'sc']:
        description_string = "{} Short".format(filetype)
    elif cadence in ['any', 'both']:
        description_string = "{}".format(filetype)
    else:
        description_string = "{} Long".format(filetype)
    mask &= np.array([description_string in desc for desc in products['description']])

    # Identify quarter by the description.
    # This is necessary because the `sequence_number` field was not populated
    # for Kepler prime data at the time of writing this function.
    if quarter is not None:
        quarter_mask = np.zeros(len(products), dtype=bool)
        for q in np.atleast_1d(quarter):
            quarter_mask |= np.array([desc.lower().replace('-', '').endswith('q{}'.format(q))
                                      for desc in products['description']])
        mask &= quarter_mask

    # For Kepler short cadence data the month can be specified
    if month is not None:
        month = np.atleast_1d(month)
        # Get the short cadence date lookup table.
        table = ascii.read(os.path.join(PACKAGEDIR, 'data', 'short_cadence_month_lookup.csv'))
        # The following line is needed for systems where the default integer type
        # is int32 (e.g. Windows/Appveyor), the column will then be interpreted
        # as string which makes the test fail.
        table['StartTime'] = table['StartTime'].astype(str)
        # Grab the dates of each of the short cadence files.
        # Make sure every entry has the correct month
        is_shortcadence = mask & np.asarray(['Short' in desc for desc in products['description']])
        for idx in np.where(is_shortcadence)[0]:
            quarter = int(products['description'][idx].split(' - ')[-1][1:].replace('-', ''))
            date = products['dataURI'][idx].split('/')[-1].split('-')[1].split('_')[0]
            permitted_dates = []
            for m in month:
                try:
                    permitted_dates.append(table['StartTime'][
                        np.where((table['Month'] == m) & (table['Quarter'] == quarter))[0][0]
                                    ])
                except IndexError:
                    pass
            if not (date in permitted_dates):
                mask[idx] = False

    return mask


def _mask_k2_products(products, campaign=None, cadence='long', filetype='Target Pixel'):
    """Returns a mask flagging the K2 products that match the criteria."""
    mask = np.array([prov.lower() == 'k2' for prov in products['provenance_name']])
    if mask.sum() == 0:
        return mask

    # Filters on cadence and product type
    if cadence in ['short', 'sc']:
        description_string = "{} Short".format(filetype)
    elif cadence in ['any', 'both']:
        description_string = "{}".format(filetype)
    else:
        description_string = "{} Long".format(filetype)
    mask &= np.array([description_string in desc for desc in products['description']])

    return mask


def _mask_spoc_products(products, sector=None, filetype='Target Pixel'):
    """Returns a mask flagging the TESS products that match the criteria."""
    mask = np.array([p.lower() == 'spoc' for p in products['provenance_name']])
    if mask.sum() == 0:
        return mask

    # Filter on product type
    if filetype.lower() == 'lightcurve':
        description_string = 'Light curves'
    elif filetype.lower() == 'target pixel':
        description_string = 'Target pixel files'
    elif filetype.lower() == 'ffi':
        description_string = 'TESScut'
    mask &= np.array([description_string in desc for desc in products['description']])

    return mask


def _resolve_object(target):
    """Ask MAST to resolve an object string to a set of coordinates."""
    from astroquery.mast import MastClass
    # Note: `_resolve_object` was renamed `resolve_object` in astroquery 0.3.10 (2019)
    return MastClass().resolve_object(target)
