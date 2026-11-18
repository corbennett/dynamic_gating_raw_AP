We are working up to uploading raw AP band ephys files from the Dynamic Gating project to S3. First we need to find the dat files for each experiment, write a timestamp file that gives each sample of the dat file a timestamp from the master (sync) clock, and validate that this worked. 

1) The nwb files for each experiment (with metadata and spiking data but NOT AP band data) can be found in this DANDIset: DANDI:001051/0.260922.1827

2) The dat files can be gotten from LIMS. To see how to find data in lims for a given mouse or session, you can refer to this github repo: np-session. Some sessions won't have dat files. You can mark these and move on.

3) to find the scaling and shift factors necessary to align the dat files to the sync clock, here's an example for one session, the others are stored similarly: /Volumes/programs-6/braintv/workgroups/nc-ophys/corbettb/NP_behavior_pipeline/QC/1244070169_642504_20230201/probeSyncAlignment/642504_20230201_probe_sync_registration.json

You can refer to npc-ephys to see how to open these dat files. They are very large, so you might want to just memory map a portion of the file for testing purposes.

The output of this project should be a clear plan on how you would upload session AP band data and an accompanying npy timestamp file to S3. This should include:

1) Generation of the timestamps for each of the dat files. You can see how to do this in npc-ephys.
2) A csv file with links to all of the relevant files for each session (if sessions aren't available they should be marked)
3) For each session you should grab a bit of the relevant dat file (there should be one for each probe) and plot a spike triggered average for one large amplitude unit that passes default_qc in the nwb file. The result should resemble the waveform stored in the nwb file. This will validate that the timing is correct.

Use uv to manage packages.