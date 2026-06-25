#######################################################
#
# PublishMpmsFileFromEaModel.py
# Python implementation to generate a predictive integration diff report of EA model after branch is merged to mainline.
# Created on:      12-feb-2022
# Original author: dashora
#
#######################################################

# Includes
import argparse
import sys
import os
import shutil
import Utils
# --------

# File version
__version__ = "1.1.0"

# argument definition
parser = argparse.ArgumentParser()
parser.add_argument("directory", help="Path of the git repo.")
parser.add_argument("diffSessionOutput", help="Path and name of the .ltsfs file of the diff with the previous commit.")
args = parser.parse_args()

directory = args.directory
diffSessionOutput = args.diffSessionOutput
# ---------------------

print(f"== MPMS Diff Report (Integration-Diff) Generator v{__version__} ==")
os.chdir(directory)

# Currently, the first found .eapx/.qea file is considered
pullRequestModelFile = Utils.GetFirstFileInFolder(directory)

if pullRequestModelFile and pullRequestModelFile != "":
    componentGuid=""
    componentFile = Utils.GetFirstFileInFolder(directory, "*.mpms")

    # take the guid via substring from e.g "Customer A-46e3f67f-4e87-c98e-129a-049eb063405f"
    componentFileBaseName = os.path.basename(componentFile)
    componentGuid = componentFileBaseName[len(componentFileBaseName)-41:len(componentFileBaseName)-5]

    # copy the component file to a temp subfolder, to have it later for the diff
    # A publish to the same folder would overwrite the file
    tempFolder = "fileBeforePublish"
    os.mkdir(tempFolder)
    shutil.copy(componentFile, f"{tempFolder}/{componentFileBaseName}")

    print(f"Exporting Components: {componentGuid}")
    exportReturnCode = Utils.LemonTreePublishComponent(pullRequestModelFile, directory, componentGuid)
    print(f"Export return code {exportReturnCode}")
    if exportReturnCode != 0 :
        sys.exit(exportReturnCode)

    # Diff component file before pull request integration with file published after pull request integration
    diffReturnCode = Utils.LemonTreeGenerateDiffSession(
        componentFile, componentFile, f"{tempFolder}/{componentFileBaseName}", diffSessionOutput
    )
    print(f"Diff return code {diffReturnCode}")
    if diffReturnCode != 0 :
        sys.exit(diffReturnCode)

    # reset the file (copy back from temp folder) to make sure the file from git is used
    # if the build is run in the same folder and is not cleaned up
    shutil.copy(f"{tempFolder}/{componentFileBaseName}", componentFile)
    shutil.rmtree(tempFolder)
else:
    print(
        f"Generated pull request model file '{pullRequestModelFile}' not found in the given folder. Aborting MPMS Publish and LT Diff."
    )
