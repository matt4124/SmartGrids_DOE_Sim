# SmartGrids_DOE_Sim



### Setup
- Setup Jupyter notebook 
- Make a python virtual environment within the /SmartGrids_DOE_Sim folder, or just use base python installation
- run "pip install -r requirements.txt" in command prompt
- Make sure PSS Sincal Version 22.5 is installed 

### File and Folder Descriptions
**/opendss_networks**
- Folder of the LV networks in OpenDSS format
- Accurate LV networks with single-phase loads*

**/sincal_networks**
- Folder of the LV networks in PSS-Sincal format
- Destination folder of opendss_to_sincal.py, saving our folders in PSS-Sincal

**/profiles**
- folder full of CSVs for our load and PV generation profiles
- Only provides real power, so reactive power is inferred from PF(may need to change if have time)

**opendss_to_sincal.py**
- Script to convert files from OpenDSS format to PSS-Sincal format
- Also adds varshunts to every node with a load, these act as generators 

**run_qsts.py**
- Run the quasi-static time series

**main_sim.ipynb** 
- Jupyter notebook where all the simulations are actually run from

**requirements.txt**
- list of all python modules that need to be installed 