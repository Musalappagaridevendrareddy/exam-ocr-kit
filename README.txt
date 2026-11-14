EXAM PAPER OCR KIT

This repo holds the code, the list of papers and 51 bundled PDFs (the 8 sample
papers plus 43 that have no download link). The other 2,143 scanned papers are
fetched from their official sites by the script, so nothing large is copied.

SAMPLE RUN (do this first)
1. Clone or download this repo on the AMD machine.
2. Double-click RUN.cmd. It needs internet the first time.
   It installs Python, the model server and four OCR models INSIDE this folder
   (about 40 GB of models), then reads the first 5 pages of 8 sample papers
   with each model.
3. When it says "Done", send back the file results_sample.zip.

FULL RUN (later, after a model is chosen)
   RUN.cmd -Mode full -Models dots -Parallel 4
It first downloads the scanned papers (about 8 GB, resumable), then reads them
all. Send back results_full.zip. If it is interrupted, run the same command
again: finished papers are skipped.
To only download the papers:  RUN.cmd -Mode download

Nothing is installed outside this folder. Delete the folder to remove it all.
If something fails, send the "logs" folder.
