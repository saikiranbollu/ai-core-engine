#######################################################
#
# PublishMpmsFileFromEaModel.py
# Python implementation to generate EA model diff report with previous commit
# Created on:      12-feb-2022
# Original author: dashora
#
#######################################################

# Includes
import argparse
import sys
import os
import subprocess
import Utils

# File version
__version__ = "2.0.0"

# argument definition
parser = argparse.ArgumentParser()
parser.add_argument("directory", help="Path of the git repo.")
parser.add_argument("diffSessionOutput", help="Path and name of the .ltsfs file of the diff with the previous commit.")
args = parser.parse_args()

directory = args.directory
diffSessionOutput = args.diffSessionOutput
# ---------------------

print(f"== MPMS Diff Report (LastCommit-Diff) Generator v{__version__} ==")
os.chdir(directory)

# get mpms file
mpmsfile = Utils.GetFirstFileInFolder(directory, "*.mpms")
tempNameOfMpms = "temp_willBeRenamed.mpms"
namePreviousCommit = "previousCommit.mpms"
originalName = os.path.basename(mpmsfile)

# rename existing file temporarily
os.rename(originalName, tempNameOfMpms)

# checkout previous commit
subprocess.call(f"git checkout -q HEAD^ \"{originalName}\"")

# rename previous commit
os.rename(originalName, namePreviousCommit)

# set main file back to correct name
os.rename(tempNameOfMpms, originalName)

# start LT diff and write session file
diffReturnCode = Utils.LemonTreeGenerateDiffSession(
    namePreviousCommit, namePreviousCommit, originalName, diffSessionOutput
)
print(f"Diff return code {diffReturnCode}")

# remove previous commit file
os.remove(namePreviousCommit)

if diffReturnCode != 0 :
    sys.exit(diffReturnCode)