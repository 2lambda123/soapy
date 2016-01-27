#Copyright Durham University and Andrew Reeves
#2014

# This file is part of soapy.

#     soapy is free software: you can redistribute it and/or modify
#     it under the terms of the GNU General Public License as published by
#     the Free Software Foundation, either version 3 of the License, or
#     (at your option) any later version.

#     soapy is distributed in the hope that it will be useful,
#     but WITHOUT ANY WARRANTY; without even the implied warranty of
#     MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#     GNU General Public License for more details.

#     You should have received a copy of the GNU General Public License
#     along with soapy.  If not, see <http://www.gnu.org/licenses/>.

"""
The Soapy WFS module.


This module contains a number of classes which simulate different adaptive optics wavefront sensor (WFS) types. All wavefront sensor classes can inherit from the base ``WFS`` class. The class provides the methods required to calculate phase over a WFS pointing in a given WFS direction and accounts for Laser Guide Star (LGS) geometry such as cone effect and elongation. This is  If only pupil images (or complex amplitudes) are required, then this class can be used stand-alone.

Example:

    Make configuration objects::

        from soapy import WFS, confParse

        config = confParse.Configurator("config_file.py")
        config.loadSimParams()

    Initialise the wave-front sensor::

        wfs = WFS.WFS(config.sim, config.wfss[0], config.atmos, config.lgss[0], mask)

    Set the WFS scrns (these should be made in advance, perhaps by the :py:mod:`soapy.atmosphere` module). Then run the WFS::

        wfs.scrns = phaseScrnList
        wfs.makePhase()

    Now you can view data from the WFS frame::

        frameEField = wfs.EField


A Shack-Hartmann WFS is also included in the module, this contains further methods to make the focal plane, then calculate the slopes to send to the reconstructor.

Example:
    Using the config objects from above...::

        shWfs = WFS.ShackHartmann(config.sim, config.wfss[0], config.atmos, config.lgss[0], mask)

    As we are using a full WFS with focal plane making methods, the WFS base classes ``frame`` method can be used to take a frame from the WFS::

        slopes = shWfs.frame(phaseScrnList)

    All the data from that WFS frame is available for inspection. For instance, to obtain the electric field across the WFS and the image seen by the WFS detector::

        EField = shWfs.EField
        wfsDetector = shWfs.wfsDetectorPlane


Adding new WFSs
^^^^^^^^^^^^^^^

New WFS classes should inherit the ``WFS`` class, then create methods which deal with creating the focal plane and making a measurement from it. To make use of the base-classes ``frame`` method, which will run the WFS entirely, the new class must contain the following methods::

    calcFocalPlane(self)
    makeDetectorPlane(self)
    calculateSlopes(self)

The Final ``calculateSlopes`` method must set ``self.slopes`` to be the measurements made by the WFS. If LGS elongation is to be used for the new WFS, create a ``detectorPlane``, which is added to for each LGS elongation propagation. Have a look at the code for the ``Shack-Hartmann`` and experimental ``Pyramid`` WFSs to get some ideas on how to do this.


:Author:
    Andrew Reeves
"""

import numpy
import numpy.random
from scipy.interpolate import interp2d
try:
    from astropy.io import fits
except ImportError:
    try:
        import pyfits as fits
    except ImportError:
        raise ImportError("PyAOS requires either pyfits or astropy")

from . import AOFFT, aoSimLib, LGS, logger, lineofsight
from .tools import centroiders
from .opticalPropagationLib import angularSpectrum

# xrange now just "range" in python3.
# Following code means fastest implementation used in 2 and 3
try:
    xrange
except NameError:
    xrange = range

# The data type of data arrays (complex and real respectively)
CDTYPE = numpy.complex64
DTYPE = numpy.float32


class WFS(object):
    ''' A  WFS class.

        This is a base class which contains methods to initialise the WFS,
        and calculate the phase across the WFSs input aperture, given the WFS
        guide star geometry.

        Parameters:


            simConfig (confObj): The simulation configuration object
            wfsConfig (confObj): The WFS configuration object
            atmosConfig (confObj): The atmosphere configuration object
            lgsConfig (confObj): The Laser Guide Star configuration
            mask (ndarray, optional): An array or size (simConfig.pupilSize, simConfig.pupilSize) which is 1 at the telescope aperture and 0 else-where.
    '''

    def __init__(
            self, simConfig, wfsConfig, atmosConfig, lgsConfig=None,
            mask=None):

        self.simConfig = simConfig
        self.config = wfsConfig
        self.atmosConfig = atmosConfig
        self.lgsConfig = lgsConfig

        # If supplied use the mask
        if numpy.any(mask):
            self.mask = mask
        # Else we'll just make a circle
        else:
            self.mask = aoSimLib.circle(
                    self.simConfig.pupilSize/2., self.simConfig.simSize,
                    )

        self.iMat = False

        # Init the line of sight
        print("Init LOS")
        self.initLos()

        self.calcInitParams()
        # If GS not at infinity, find meta-pupil radii for each layer
        if self.config.GSHeight != 0:
            self.radii = self.los.findMetaPupilSize(self.config.GSHeight)
        else:
            self.radii = None

        # Init LGS, FFTs and allocate some data arrays
        self.initFFTs()
        if self.lgsConfig and self.config.lgs:
            self.initLGS()

        self.allocDataArrays()

        self.calcTiltCorrect()
        self.getStatic()

############################################################
# Initialisation routines

    def calcInitParams(self, phaseSize=None):
        self.los.calcInitParams(phaseSize)

    def initFFTs(self):
        pass

    def allocDataArrays(self):
        pass

    def initLos(self):
        """
        Initialises the ``LineOfSight`` object, which gets the phase or EField in a given direction through turbulence.
        """
        self.los = lineofsight.LineOfSight(
                self.config, self.simConfig, self.atmosConfig,
                outPxlScale=self.simConfig.pxlScale**-1, 
                propagationDirection="down")

    def initLGS(self):
        """
        Initialises the LGS objects for the WFS

        Creates and initialises the LGS objects if the WFS GS is a LGS. This
        included calculating the phases additions which are required if the
        LGS is elongated based on the depth of the elongation and the launch
        position. Note that if the GS is at infinity, elongation is not possible
        and a warning is logged.
        """

        # Choose the correct LGS object, either with physical or geometric
        # or geometric propagation.
        if self.lgsConfig.uplink:

            self.lgs = LGS.LGS(
                    self.simConfig, self.config, self.lgsConfig, 
                    self.atmosConfig)
        else:
            self.LGS = None

        self.lgsLaunchPos = None
        self.elong = 0
        self.elongLayers = 0
        if self.config.lgs:
            self.lgsLaunchPos = self.lgsConfig.launchPosition
            # LGS Elongation##############################
            if (self.config.GSHeight!=0 and
                    self.lgsConfig.elongationDepth!=0):
                self.elong = self.lgsConfig.elongationDepth
                self.elongLayers = self.lgsConfig.elongationLayers

                # Get Heights of elong layers
                self.elongHeights = numpy.linspace(
                    self.config.GSHeight-self.elong/2.,
                    self.config.GSHeight+self.elong/2.,
                    self.elongLayers
                    )

                # Calculate the zernikes to add
                self.elongZs = aoSimLib.zernikeArray([2,3,4], self.simConfig.pupilSize)

                # Calculate the radii of the metapupii at for different elong
                # Layer heights
                # Also calculate the required phase addition for each layer
                self.elongRadii = {}
                self.elongPos = {}
                self.elongPhaseAdditions = numpy.zeros(
                    (self.elongLayers, self.los.outPhaseSize, self.los.PhaseSize))
                for i in xrange(self.elongLayers):
                    self.elongRadii[i] = self.findMetaPupilSize(
                                                float(self.elongHeights[i]))
                    self.elongPhaseAdditions[i] = self.calcElongPhaseAddition(i)
                    self.elongPos[i] = self.calcElongPos(i)

            # If GS at infinity cant do elongation
            elif (self.config.GSHeight==0 and
                    self.lgsConfig.elongationDepth!=0):
                logger.warning("Not able to implement LGS Elongation as GS at infinity")

    def calcTiltCorrect(self):
        pass

    def getStatic(self):
        self.staticData = None

    def calcElongPhaseAddition(self, elongLayer):
        """
        Calculates the phase required to emulate layers on an elongated source

        For each 'elongation layer' a phase addition is calculated which
        accounts for the difference in height from the nominal GS height where
        the WFS is focussed, and accounts for the tilt seen if the LGS is
        launched off-axis.

        Parameters:
            elongLayer (int): The number of the elongation layer

        Returns:
            ndarray: The phase addition required for that layer.
        """

        # Calculate the path difference between the central GS height and the
        # elongation "layer"
        # Define these to make it easier
        h = self.elongHeights[elongLayer]
        dh = h - self.config.GSHeight
        H = self.lgsConfig.height
        d = numpy.array(self.lgsLaunchPos).astype('float32') * self.los.telDiam/2.
        D = self.los.telDiam
        theta = (d.astype("float")/H) - self.config.GSPosition


        # for the focus terms....
        focalPathDiff = (2*numpy.pi/self.wfsConfig.wavelength) * ((
            ((self.telDiam/2.)**2 + (h**2) )**0.5\
          - ((self.telDiam/2.)**2 + (H)**2 )**0.5 ) - dh)

        # For tilt terms.....
        tiltPathDiff = (2*numpy.pi/self.wfsConfig.wavelength) * (
                numpy.sqrt( (dh+H)**2. + ( (dh+H)*theta-d-D/2.)**2 )
                + numpy.sqrt( H**2 + (D/2. - d + H*theta)**2 )
                - numpy.sqrt( H**2 + (H*theta - d - D/2.)**2)
                - numpy.sqrt( (dh+H)**2 + (D/2. - d + (dh+H)*theta )**2))


        phaseAddition = numpy.zeros(
                    (self.simConfig.pupilSize, self.simConfig.pupilSize))

        phaseAddition +=((self.elongZs[2]/self.elongZs[2].max())
                             * focalPathDiff )
        # X,Y tilt
        phaseAddition += ((self.elongZs[0]/self.elongZs[0].max())
                            *tiltPathDiff[0] )
        phaseAddition += ((self.elongZs[1]/self.elongZs[1].max())
                            *tiltPathDiff[1])

        # Pad from pupilSize to simSize
        pad = ((self.simConfig.simPad,)*2, (self.simConfig.simPad,)*2)
        phaseAddition = numpy.pad(phaseAddition, pad, mode="constant")

        phaseAddition = aoSimLib.zoom(phaseAddition, self.los.outPhaseSize)

        return phaseAddition

    def calcElongPos(self, elongLayer):
        """
        Calculates the difference in GS position for each elongation layer
        only makes a difference if LGS launched off-axis

        Parameters:
            elongLayer (int): which elongation layer

        Returns:
            float: The effect position of that layer GS
        """

        h = self.elongHeights[elongLayer]       #height of elonglayer
        dh = h - self.config.GSHeight          #delta height from GS Height
        H = self.config.GSHeight               #Height of GS

        #Position of launch in m
        xl = numpy.array(self.lgsLaunchPos) * self.los.telDiam/2.

        #GS Pos in radians
        GSPos = numpy.array(self.config.GSPosition)*numpy.pi/(3600.0*180.0)

        #difference in angular Pos for that height layer in rads
        theta_n = GSPos - ((dh*xl)/ (H*(H+dh)))

        return theta_n

    def zeroPhaseData(self):
        self.los.EField[:] = 0
        self.los.phase[:] = 0

    def frame(self, scrns, correction=None, read=True, iMatFrame=False):
        '''
        Runs one WFS frame

        Runs a single frame of the WFS with a given set of phase screens and
        some optional correction. If elongation is set, will run the phase
        calculating and focal plane making methods multiple times for a few
        different heights of LGS, then sum these onto a ``wfsDetectorPlane``.

        Parameters:
            scrns (list): A list or dict containing the phase screens
            correction (ndarray, optional): The correction term to take from the phase screens before the WFS is run.
            read (bool, optional): Should the WFS be read out? if False, then WFS image is calculated but slopes not calculated. defaults to True.
            iMatFrame (bool, optional): If True, will assume an interaction matrix is being measured. Turns off some AO loop features before running

        Returns:
            ndarray: WFS Measurements
        '''

       #If iMatFrame, turn off unwanted effects
        if iMatFrame:
            self.iMat = True
            removeTT = self.config.removeTT
            self.config.removeTT = False
            if self.config.lgs:
                elong = self.elong
            self.elong = 0
            photonNoise = self.config.photonNoise
            self.config.photonNoise = False
            eReadNoise = self.config.eReadNoise
            self.config.eReadNoise = 0

        self.zeroData(detector=read, inter=False)

        self.los.frame(scrns)

        # If LGS elongation simulated
        if self.config.lgs and self.elong!=0:
            for i in xrange(self.elongLayers):
                self.zeroPhaseData()

                self.los.makePhase(self.elongRadii[i], self.elongPos[i])
                self.uncorrectedPhase = self.los.phase.copy()/self.los.phs2Rad
                self.los.EField *= numpy.exp(1j*self.elongPhaseAdditions[i])
                if numpy.any(correction):
                    self.los.EField *= numpy.exp(-1j*correction*self.los.phs2Rad)
                self.calcFocalPlane(intensity=self.lgsConfig.naProfile[i])

        # If no elongation
        else:
            # If imat frame, dont want to make it off-axis
            if iMatFrame:
                try:
                    iMatPhase = aoSimLib.zoom(scrns, self.los.outPhaseSize, order=1)
                    self.los.EField[:] = numpy.exp(1j*iMatPhase*self.los.phs2Rad)
                except ValueError:
                    raise ValueError("If iMat Frame, scrn must be ``simSize``")
            else:
                self.los.makePhase(self.radii)

            self.uncorrectedPhase = self.los.phase.copy()/self.los.phs2Rad
            if numpy.any(correction):
                correctionPhase = aoSimLib.zoom(
                        correction, self.los.outPhaseSize, order=1)
                self.los.EField *= numpy.exp(-1j*correctionPhase*self.los.phs2Rad)
            self.calcFocalPlane()

        if read:
            self.makeDetectorPlane()
            self.calculateSlopes()
            self.zeroData(detector=False)

        #Turn back on stuff disabled for iMat
        if iMatFrame:
            self.iMat=False
            self.config.removeTT = removeTT
            if self.config.lgs:
                self.elong = elong
            self.config.photonNoise = photonNoise
            self.config.eReadNoise = eReadNoise

        # Check that slopes aint `nan`s. Set to 0 if so
        if numpy.any(numpy.isnan(self.slopes)):
            self.slopes[:] = 0

        return self.slopes

    def addPhotonNoise(self):
        """
        Add photon noise to ``wfsDetectorPlane`` using ``numpy.random.poisson``
        """
        self.wfsDetectorPlane = numpy.random.poisson(
                self.wfsDetectorPlane).astype(DTYPE)


    def addReadNoise(self):
        """
        Adds read noise to ``wfsDetectorPlane using ``numpy.random.normal``.
        This generates a normal (guassian) distribution of random numbers to
        add to the detector. Any CCD bias is assumed to have been removed, so
        the distribution is centred around 0. The width of the distribution
        is determined by the value `eReadNoise` set in the WFS configuration.
        """
        self.wfsDetectorPlane += numpy.random.normal(
                0, self.config.eReadNoise, self.wfsDetectorPlane.shape
                )


    def calcFocalPlane(self):
        pass

    def makeDetectorPlane(self):
        pass

    def LGSUplink(self):
        pass

    def calculateSlopes(self):
        self.slopes = self.los.EField

    def zeroData(self, detector=True, inter=True):
        self.zeroPhaseData()

#   _____ _   _
#  /  ___| | | |
#  \ `--.| |_| |
#   `--. \  _  |
#  /\__/ / | | |
#  \____/\_| |_/
class ShackHartmann(WFS):
    """Class to simulate a Shack-Hartmann WFS"""


    def calcInitParams(self):
        """
        Calculate some parameters to be used during initialisation
        """
        super(ShackHartmann, self).calcInitParams()

        self.subapFOVrad = self.config.subapFOV * numpy.pi / (180. * 3600)
        self.subapDiam = self.los.telDiam/self.config.nxSubaps

        # spacing between subaps in pupil Plane (size "pupilSize")
        self.PPSpacing = float(self.simConfig.pupilSize)/self.config.nxSubaps

        # Spacing on the "FOV Plane" - the number of elements required
        # for the correct subap FOV (from way FFT "phase" to "image" works)
        self.subapFOVSpacing = numpy.round(self.subapDiam
                                * self.subapFOVrad/ self.config.wavelength)

        # make twice as big to double subap FOV
        if self.config.subapFieldStop==True:
            self.SUBAP_OVERSIZE = 1
        else:
            self.SUBAP_OVERSIZE = 2

        self.detectorPxls = self.config.pxlsPerSubap*self.config.nxSubaps
        self.subapFOVSpacing *= self.SUBAP_OVERSIZE
        self.config.pxlsPerSubap2 = (self.SUBAP_OVERSIZE
                                            *self.config.pxlsPerSubap)

        self.scaledEFieldSize = int(round(
                self.config.nxSubaps*self.subapFOVSpacing*
                (float(self.simConfig.simSize)/self.simConfig.pupilSize)
                ))
        print(self.simConfig.simSize)
        print(self.scaledEFieldSize)
        print(self.simConfig.pxlScale)
        outPxlScale = (float(self.simConfig.simSize)/float(self.scaledEFieldSize)) * (self.simConfig.pxlScale**-1)
        print(outPxlScale)
        self.los.calcInitParams(outPxlScale=outPxlScale)

        # Calculate the subaps which are actually seen behind the pupil mask
        self.findActiveSubaps()

        # For correlation centroider, open reference image.
        if self.config.centMethod=="correlation":
            rawRef = fits.open("./conf/correlationRef/"+self.config.referenceImage)[0].data
            self.config.referenceImage = numpy.zeros((self.activeSubaps,
                    self.config.pxlsPerSubap, self.config.pxlsPerSubap))
            for i in range(self.activeSubaps):
                self.config.referenceImage[i] = rawRef[
                        self.detectorSubapCoords[i, 0]:
                        self.detectorSubapCoords[i, 0]+self.config.pxlsPerSubap,
                        self.detectorSubapCoords[i, 1]:
                        self.detectorSubapCoords[i, 1]+self.config.pxlsPerSubap]


    def findActiveSubaps(self):
        '''
        Finds the subapertures which are not empty space
        determined if mean of subap coords of the mask is above threshold.

        '''

        mask = self.mask[
                self.simConfig.simPad : -self.simConfig.simPad,
                self.simConfig.simPad : -self.simConfig.simPad
                ]
        self.subapCoords, self.subapFillFactor = aoSimLib.findActiveSubaps(
                self.config.nxSubaps, mask,
                self.config.subapThreshold, returnFill=True)

        self.activeSubaps = self.subapCoords.shape[0]
        self.detectorSubapCoords = numpy.round(
                self.subapCoords*(
                        self.detectorPxls/float(self.simConfig.pupilSize) ) )

        #Find the mask to apply to the scaled EField
        self.scaledMask = numpy.round(aoSimLib.zoom(
                    self.mask, self.scaledEFieldSize))


    def initFFTs(self):
        """
        Initialise the FFT Objects required for running the WFS

        Initialised various FFT objects which are used through the WFS,
        these include FFTs to calculate focal planes, and to convolve LGS
        PSFs with the focal planes
        """

        #Calculate the FFT padding to use
        self.subapFFTPadding = self.config.pxlsPerSubap2 * self.config.fftOversamp
        if self.subapFFTPadding < self.subapFOVSpacing:
            while self.subapFFTPadding<self.subapFOVSpacing:
                self.config.fftOversamp+=1
                self.subapFFTPadding\
                        =self.config.pxlsPerSubap2*self.config.fftOversamp

            logger.warning("requested WFS FFT Padding less than FOV size... Setting oversampling to: %d"%self.config.fftOversamp)

        #Init the FFT to the focal plane
        self.FFT = AOFFT.FFT(
                inputSize=(
                self.activeSubaps, self.subapFFTPadding, self.subapFFTPadding),
                axes=(-2,-1), mode="pyfftw",dtype=CDTYPE,
                THREADS=self.config.fftwThreads,
                fftw_FLAGS=(self.config.fftwFlag,"FFTW_DESTROY_INPUT"))

        #If LGS uplink, init FFTs to conovolve LGS PSF and WFS PSF(s)
        #This works even if no lgsConfig.uplink as ``and`` short circuits
        if self.lgsConfig and self.lgsConfig.uplink:
            self.iFFT = AOFFT.FFT(
                    inputSize = (self.activeSubaps,
                                        self.subapFFTPadding,
                                        self.subapFFTPadding),
                    axes=(-2, -1), mode="pyfftw", dtype=CDTYPE,
                    THREADS=self.config.fftwThreads,
                    fftw_FLAGS=(self.config.fftwFlag,"FFTW_DESTROY_INPUT")
                    )

            self.lgs_iFFT = AOFFT.FFT(
                    inputSize = (self.subapFFTPadding,
                                self.subapFFTPadding),
                    axes=(0,1), mode="pyfftw", dtype=CDTYPE,
                    THREADS=self.config.fftwThreads,
                    fftw_FLAGS=(self.config.fftwFlag,"FFTW_DESTROY_INPUT")
                    )

    def allocDataArrays(self):
        """
        Allocate the data arrays the WFS will require

        Determines and allocates the various arrays the WFS will require to
        avoid having to re-alloc memory during the running of the WFS and
        keep it fast.
        """
        self.los.allocDataArrays()
        self.subapArrays=numpy.zeros((self.activeSubaps,
                                      self.subapFOVSpacing,
                                      self.subapFOVSpacing),
                                     dtype=CDTYPE)
        self.binnedFPSubapArrays = numpy.zeros( (self.activeSubaps,
                                                self.config.pxlsPerSubap2,
                                                self.config.pxlsPerSubap2),
                                                dtype=DTYPE)
        self.FPSubapArrays = numpy.zeros((self.activeSubaps,
                                          self.subapFFTPadding,
                                          self.subapFFTPadding),dtype=DTYPE)

        self.maxFlux = 0.7 * 2**self.config.bitDepth -1
        self.wfsDetectorPlane = numpy.zeros( (  self.detectorPxls,
                                                self.detectorPxls   ),
                                                dtype = DTYPE )
        #Array used when centroiding subaps
        self.centSubapArrays = numpy.zeros( (self.activeSubaps,
              self.config.pxlsPerSubap, self.config.pxlsPerSubap) )

        self.slopes = numpy.zeros( 2*self.activeSubaps )

    def initLGS(self):
        super(ShackHartmann, self).initLGS()


    def calcTiltCorrect(self):
        """
        Calculates the required tilt to add to avoid the PSF being centred on
        only 1 pixel
        """
        if not self.config.pxlsPerSubap%2:
            # If pxlsPerSubap is even
            # Angle we need to correct for half a pixel
            theta = self.SUBAP_OVERSIZE*self.subapFOVrad/ (
                    2*self.subapFFTPadding)

            # Magnitude of tilt required to get that angle
            A = theta * self.subapDiam/(2*self.config.wavelength)*2*numpy.pi

            # Create tilt arrays and apply magnitude
            coords = numpy.linspace(-1, 1, self.subapFOVSpacing)
            X,Y = numpy.meshgrid(coords,coords)

            self.tiltFix = -1 * A * (X+Y)

        else:
            self.tiltFix = numpy.zeros((self.subapFOVSpacing,)*2)

    def oneSubap(self, phs):
        '''
        Processes one subaperture only, with given phase
        '''
        EField = numpy.exp(1j*phs)
        FP = numpy.abs(numpy.fft.fftshift(
            numpy.fft.fft2(EField * numpy.exp(1j*self.XTilt),
                s=(self.subapFFTPadding,self.subapFFTPadding))))**2

        FPDetector = aoSimLib.binImgs(FP,self.config.fftOversamp)

        slope = aoSimLib.simpleCentroid(FPDetector,
                    self.config.centThreshold)
        slope -= self.config.pxlsPerSubap2/2.
        return slope


    def getStatic(self):
        """
        Computes the static measurements, i.e., slopes with flat wavefront
        """

        self.staticData = None

        #Make flat wavefront, and run through WFS in iMat mode to turn off features
        phs = numpy.zeros([self.simConfig.simSize]*2).astype(DTYPE)
        self.staticData = self.frame(
                phs, iMatFrame=True).copy().reshape(2,self.activeSubaps)
#######################################################################


    def zeroData(self, detector=True, inter=True):
        """
        Sets data structures in WFS to zero.

        Parameters:
            detector (bool, optional): Zero the detector? default:True
            inter (bool, optional): Zero intermediate arrays? default: True
        """

        self.zeroPhaseData()

        if inter:
            self.FPSubapArrays[:] = 0

        if detector:
            self.wfsDetectorPlane[:] = 0


    def calcFocalPlane(self, intensity=1):
        '''
        Calculates the wfs focal plane, given the phase across the WFS
        '''

        #Scale phase (EField) to correct size for FOV (plus a bit with padding)
        # self.scaledEField = aoSimLib.zoom(
        #         self.los.EField, self.scaledEFieldSize)*self.scaledMask

        #Now cut out only the eField across the pupilSize
        coord = round(int(((self.scaledEFieldSize/2.)
                - (self.config.nxSubaps*self.subapFOVSpacing)/2.)))
        self.cropEField = self.los.EField[coord:-coord, coord:-coord]

        #create an array of individual subap EFields
        for i in xrange(self.activeSubaps):
            x,y = numpy.round(self.subapCoords[i] *
                                     self.subapFOVSpacing/self.PPSpacing)
            self.subapArrays[i] = self.cropEField[
                                    int(x):
                                    int(x+self.subapFOVSpacing) ,
                                    int(y):
                                    int(y+self.subapFOVSpacing)]

        #do the fft to all subaps at the same time
        # and convert into intensity
        self.FFT.inputData[:] = 0
        self.FFT.inputData[:,:int(round(self.subapFOVSpacing))
                        ,:int(round(self.subapFOVSpacing))] \
                = self.subapArrays*numpy.exp(1j*(self.tiltFix))


        if intensity==1:
            self.FPSubapArrays += numpy.abs(AOFFT.ftShift2d(self.FFT()))**2
        else:
            self.FPSubapArrays += intensity*numpy.abs(
                    AOFFT.ftShift2d(self.FFT()))**2


    def makeDetectorPlane(self):
        '''
        Scales and bins intensity data onto the detector with a given number of
        pixels.

        If required, will first convolve final PSF with LGS PSF, then bin
        PSF down to detector size. Finally puts back into ``wfsFocalPlane``
        array in correct order.
        '''

        #If required, convolve with LGS PSF
        if self.config.lgs and self.lgs and self.lgsConfig.uplink and self.iMat!=True:
            self.LGSUplink()


        #bins back down to correct size and then
        #fits them back in to a focal plane array
        self.binnedFPSubapArrays[:] = aoSimLib.binImgs(self.FPSubapArrays,
                                            self.config.fftOversamp)

        self.binnedFPSubapArrays[:] \
                = self.maxFlux\
                        * (self.binnedFPSubapArrays.T
                            /self.binnedFPSubapArrays.max((1,2))).T
        # Scale each sub-ap flux by sub-aperture fill-factor
        self.binnedFPSubapArrays\
                = (self.binnedFPSubapArrays.T * self.subapFillFactor).T

        for i in xrange(self.activeSubaps):
            x,y=self.detectorSubapCoords[i]

            #Set default position to put arrays into (SUBAP_OVERSIZE FOV)
            x1 = int(round(
                    x+self.config.pxlsPerSubap/2.
                    -self.config.pxlsPerSubap2/2.))
            x2 = int(round(
                    x+self.config.pxlsPerSubap/2.
                    +self.config.pxlsPerSubap2/2.))
            y1 = int(round(
                    y+self.config.pxlsPerSubap/2.
                    -self.config.pxlsPerSubap2/2.))
            y2 = int(round(
                    y+self.config.pxlsPerSubap/2.
                    +self.config.pxlsPerSubap2/2.))

            #Set defualt size of input array (i.e. all of it)
            x1_fp = int(0)
            x2_fp = int(round(self.config.pxlsPerSubap2))
            y1_fp = int(0)
            y2_fp = int(round(self.config.pxlsPerSubap2))

            # If at the edge of the field, may only fit a fraction in
            if x == 0:
                x1 = 0
                x1_fp = int(round(
                        self.config.pxlsPerSubap2/2.
                        -self.config.pxlsPerSubap/2.))

            elif x == (self.detectorPxls-self.config.pxlsPerSubap):
                x2 = int(round(self.detectorPxls))
                x2_fp = int(round(
                        self.config.pxlsPerSubap2/2.
                        +self.config.pxlsPerSubap/2.))

            if y == 0:
                y1 = 0
                y1_fp = int(round(
                        self.config.pxlsPerSubap2/2.
                        -self.config.pxlsPerSubap/2.))

            elif y == (self.detectorPxls-self.config.pxlsPerSubap):
                y2 = int(self.detectorPxls)
                y2_fp = int(round(
                        self.config.pxlsPerSubap2/2.
                        +self.config.pxlsPerSubap/2.))

            self.wfsDetectorPlane[x1:x2, y1:y2] += (
                    self.binnedFPSubapArrays[i, x1_fp:x2_fp, y1_fp:y2_fp])

        # Scale data for correct number of photons
        self.wfsDetectorPlane /= self.wfsDetectorPlane.sum()
        self.wfsDetectorPlane *= aoSimLib.photonsPerMag(
                self.config.GSMag, self.mask, self.simConfig.pxlScale**(-1),
                self.config.wvlBandWidth, self.config.exposureTime
                ) * self.config.throughput

        if self.config.photonNoise:
            self.addPhotonNoise()

        if self.config.eReadNoise!=0:
            self.addReadNoise()

    def LGSUplink(self):
        '''
        A method to deal with convolving the LGS PSF
        with the subap focal plane.
        '''

        self.lgs.getLgsPsf(self.scrns)

        self.lgs_iFFT.inputData[:] = self.lgs.PSF
        self.iFFTLGSPSF = self.lgs_iFFT()

        self.iFFT.inputData[:] = self.FPSubapArrays
        self.iFFTFPSubapsArray = self.iFFT()

        # Do convolution
        self.iFFTFPSubapsArray *= self.iFFTLGSPSF

        # back to Focal Plane.
        self.FFT.inputData[:] = self.iFFTFPSubapsArray
        self.FPSubapArrays[:] = AOFFT.ftShift2d(self.FFT()).real

    def calculateSlopes(self):
        '''
        Calculates WFS slopes from wfsFocalPlane

        Returns:
            ndarray: array of all WFS measurements
        '''

        # Sort out FP into subaps
        for i in xrange(self.activeSubaps):
            x, y = self.detectorSubapCoords[i]
            x = int(x)
            y = int(y)
            self.centSubapArrays[i] = self.wfsDetectorPlane[x:x+self.config.pxlsPerSubap,
                                                    y:y+self.config.pxlsPerSubap ].astype(DTYPE)

        slopes = eval("centroiders."+self.config.centMethod)(
                self.centSubapArrays,
                threshold=self.config.centThreshold,
                ref=self.config.referenceImage
                     )


        # shift slopes relative to subap centre and remove static offsets
        slopes -= self.config.pxlsPerSubap/2.0

        if numpy.any(self.staticData):
            slopes -= self.staticData

        self.slopes[:] = slopes.reshape(self.activeSubaps*2)

        if self.config.removeTT == True:
            self.slopes[:self.activeSubaps] -= self.slopes[:self.activeSubaps].mean()
            self.slopes[self.activeSubaps:] -= self.slopes[self.activeSubaps:].mean()

        if self.config.angleEquivNoise and not self.iMat:
            pxlEquivNoise = (
                    self.config.angleEquivNoise *
                    float(self.config.pxlsPerSubap)
                    /self.config.subapFOV )
            self.slopes += numpy.random.normal( 0, pxlEquivNoise,
                                                2*self.activeSubaps)

        return self.slopes


#  ______                          _     _
#  | ___ \                        (_)   | |
#  | |_/ /   _ _ __ __ _ _ __ ___  _  __| |
#  |  __/ | | | '__/ _` | '_ ` _ \| |/ _` |
#  | |  | |_| | | | (_| | | | | | | | (_| |
#  \_|   \__, |_|  \__,_|_| |_| |_|_|\__,_|
#         __/ |
#        |___/

class Pyramid(WFS):
    """
    *Experimental* Pyramid WFS.

    This is an early prototype for a Pyramid WFS. Currently, its at a very early stage. It doesn't oscillate, so performance aint too good at the minute.

    To use, set the wfs parameter ``type'' to ``Pyramid'' type is a list of length number of wfs.
    """
    # oversampling for the first FFT from EField to focus (4 seems ok...)
    FOV_OVERSAMP = 4

    def calcInitParams(self):
        super(Pyramid, self).calcInitParams()
        self.FOVrad = self.config.subapFOV * numpy.pi / (180. * 3600)

        self.FOVPxlNo = numpy.round(self.los.telDiam *
                                    self.FOVrad/self.config.wavelength)

        self.detectorPxls = 2*self.config.pxlsPerSubap
        self.scaledMask = aoSimLib.zoom(self.mask, self.FOVPxlNo)

        self.activeSubaps = self.config.pxlsPerSubap**2

        while (self.config.pxlsPerSubap*self.config.fftOversamp
                    < self.FOVPxlNo):
            self.config.fftOversamp += 1

    def initFFTs(self):

        self.FFT = AOFFT.FFT(   [self.FOV_OVERSAMP*self.FOVPxlNo,]*2,
                                axes=(0,1), mode="pyfftw",
                                fftw_FLAGS=("FFTW_DESTROY_INPUT",
                                            self.config.fftwFlag),
                                THREADS=self.config.fftwThreads
                                )

        self.iFFTPadding = self.FOV_OVERSAMP*(self.config.fftOversamp*
                                            self.config.pxlsPerSubap)
        self.iFFT = AOFFT.FFT(
                    [4, self.iFFTPadding, self.iFFTPadding],
                    axes=(1,2), mode="pyfftw",
                    THREADS = self.config.fftwThreads,
                    fftw_FLAGS=("FFTW_DESTROY_INPUT", self.config.fftwFlag),
                    direction="BACKWARD"
                    )

    def allocDataArrays(self):

        super(Pyramid, self).allocDataArrays()
        # Allocate arrays
        # Find sizes of detector planes

        self.paddedDetectorPxls = (2*self.config.pxlsPerSubap
                                    *self.config.fftOversamp)
        self.paddedDetectorPlane = numpy.zeros([self.paddedDetectorPxls]*2,
                                                dtype=DTYPE)

        self.focalPlane = numpy.zeros( [self.FOV_OVERSAMP*self.FOVPxlNo,]*2,
                                        dtype=CDTYPE)

        self.quads = numpy.zeros(
                    (4,self.focalPlane.shape[0]/2.,self.focalPlane.shape[1]/2.),
                    dtype=CDTYPE)

        self.wfsDetectorPlane = numpy.zeros([self.detectorPxls]*2,
                                            dtype=DTYPE)

        self.slopes = numpy.zeros(2*self.activeSubaps)

    def zeroData(self, detector=True, inter=True):
        """
        Sets data structures in WFS to zero.

        Parameters:
            detector (bool, optional): Zero the detector? default:True
            inter (bool, optional): Zero intermediate arrays? default:True
        """

        self.zeroPhaseData()

        if inter:
            self.paddedDetectorPlane[:] = 0

        if detector:
            self.wfsDetectorPlane[:] = 0

    def calcFocalPlane(self):
        '''
        takes the calculated pupil phase, and uses FFT
        to transform to the focal plane, and scales for correct FOV.
        '''
        # Apply tilt fix and scale EField for correct FOV
        self.pupilEField = self.los.EField[
                self.simConfig.simPad:-self.simConfig.simPad,
                self.simConfig.simPad:-self.simConfig.simPad
                ]
        self.pupilEField *= numpy.exp(1j*self.tiltFix)
        self.scaledEField = aoSimLib.zoom(
                self.pupilEField, self.FOVPxlNo)*self.scaledMask

        # Go to the focus
        self.FFT.inputData[:] = 0
        self.FFT.inputData[ :self.FOVPxlNo,
                            :self.FOVPxlNo ] = self.scaledEField
        self.focalPlane[:] = AOFFT.ftShift2d( self.FFT() )

        #Cut focus into 4
        shapeX, shapeY = self.focalPlane.shape
        n=0
        for x in xrange(2):
            for y in xrange(2):
                self.quads[n] = self.focalPlane[x*shapeX/2 : (x+1)*shapeX/2,
                                                y*shapeX/2 : (y+1)*shapeX/2]
                n+=1

        #Propogate each quadrant back to the pupil plane
        self.iFFT.inputData[:] = 0
        self.iFFT.inputData[:,
                            :0.5*self.FOV_OVERSAMP*self.FOVPxlNo,
                            :0.5*self.FOV_OVERSAMP*self.FOVPxlNo] = self.quads
        self.pupilImages = abs(AOFFT.ftShift2d(self.iFFT()))**2

        size = self.paddedDetectorPxls/2
        pSize = self.iFFTPadding/2.


        #add this onto the padded detector array
        for x in range(2):
            for y in range(2):
                self.paddedDetectorPlane[
                        x*size:(x+1)*size,
                        y*size:(y+1)*size] += self.pupilImages[
                                                2*x+y,
                                                pSize:pSize+size,
                                                pSize:pSize+size]

    def makeDetectorPlane(self):

        #Bin down to requried pixels
        self.wfsDetectorPlane[:] += aoSimLib.binImgs(
                        self.paddedDetectorPlane,
                        self.config.fftOversamp
                        )

    def calculateSlopes(self):

        xDiff = (self.wfsDetectorPlane[ :self.config.pxlsPerSubap,:]-
                    self.wfsDetectorPlane[  self.config.pxlsPerSubap:,:])
        xSlopes = (xDiff[:,:self.config.pxlsPerSubap]
                    +xDiff[:,self.config.pxlsPerSubap:])

        yDiff = (self.wfsDetectorPlane[:, :self.config.pxlsPerSubap]-
                    self.wfsDetectorPlane[:, self.config.pxlsPerSubap:])
        ySlopes = (yDiff[:self.config.pxlsPerSubap, :]
                    +yDiff[self.config.pxlsPerSubap:, :])


        self.slopes[:] = numpy.append(xSlopes.flatten(), ySlopes.flatten())

    #Tilt optimisation
    ################################
    def calcTiltCorrect(self):
        """
        Calculates the required tilt to add to avoid the PSF being centred on
        only 1 pixel
        """
        if not self.config.pxlsPerSubap%2:
            #Angle we need to correct
            theta = self.FOVrad/ (2*self.FOV_OVERSAMP*self.FOVPxlNo)

            A = theta*self.los.telDiam/(2*self.config.wavelength)*2*numpy.pi

            coords = numpy.linspace(-1,1,self.simConfig.pupilSize)
            X,Y = numpy.meshgrid(coords,coords)

            self.tiltFix = -1*A*(X+Y)

        else:
            self.tiltFix = numpy.zeros((self.simConfig.pupilSize,)*2)
