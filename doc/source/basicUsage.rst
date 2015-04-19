Basic Usage
***********

This section describes how to the simulation for basic cases, that is, using the full end to end code to create and save data which can then be analysed afterwards. Such a scenario is a common one when exploring parameters on conventional AO systems.

Configuration
-------------

In PyAOS, all AO parameters are controlled from the configuration file. This is a python script which contains all the information required to run many AO configurations. A few examples are provided in the ``conf`` directory when you download the code. All parameters are held in one large dictionary, titled ``simConfiguration``, and  are then grouped into relavent sections.

``Sim`` parameters control simulation wide parameters, such as the filename to save data, the number of simulated phase points, the number of WFSs, DMs and Science cameras as well as the name of the reconstructor used to tie them together. The ``filePrefix`` parameters specifies a directory, which will be created if it does not already exist, where all AO run data will be recorderd. Each run will create a new time-stamped directory within the parent ``filePrefix`` one to save run specific data. Data applying to all runs, such as the interaction and control matrices are stored in the ``filePrefix`` directory.

``Atmosphere`` parameters are responsible for the structure of the simulated atmosphere. This includes the number of simulated turbulence layers and the integrated seeing strength, r\ :sub:`0`. Some values in the Atmosphere group must be formatted as a list or array, as they describe parameters which apply to different turbulence layers.

Parameters describing the physical telescope are given in the ``Telescope`` group. These include the telescope and central obscuration diameters, and a pupil mask.

WFSs, LGSs, DMs and Science camera are configured by the ``WFS``, ``LGS``, ``DM`` and ``Science`` parameter groups. As multiple instances of each of these components may be present, every parameters in these groups is represented by either a list or numpy array, where each element specifies that component number. For WFSs and DMs, a ``type`` parameter is also given. This is a the name of the python object which will be used to represent that component, and a class of the same name must be present in the ``WFS.py`` or ``DM.py`` module, respectively. Other WFS or DM parameters may then have different behaviours depending on the type which is to be used.


Each parameter that can be set is described in the :ref:`configuration` section.

Creating Phase Screens
----------------------

For most applications of PyAOS, some randomly generated phase screens are required. These can either be created just before the simulation begins, during the initialisation phase, or some existing screens can be specified for the simulation to use. To generate new phase screens with the parameters specified in ``Atmosphere`` each time the simulation is run, set the ``Atmosphere`` parameter, ``newScreens`` to ``True``. 

If instead you wish to used existing phase screens, provide the path to, and filename of each sreen in the ``screenNames`` parameter as a list. Screens specified to be loaded must be saved as FITS files, where each file contains a single, 2 dimensional phase screen. The simulation will largely trust that the screen parameters are valid, so other parameters in the ``Atmosphere`` group, such as the ``wholeScreenSize``, ``r0`` and ``L0`` may be discounted. If you would like the simulation to be able to scale your phase screens such that they adhere to the ``r0`` and ``screenStrength`` values set in the configuration file, then the FITS file header must contain a parameter ``R0`` which is expressed in units of phase pixels.


