#######################################################
#
# PublishMpmsFileFromEaModel.py
# Python implementation to publish Mpms file from an EA Model
# Created on:      12-feb-2022
# Original author: dashora
#
#######################################################

# Includes
import argparse
import os
import Utils
# --------

# File version
__version__ = "3.2.0"

# argument definition
parser = argparse.ArgumentParser(description="Publish EA Model Script")
parser.add_argument("-f", "--pathToEaFile", help="Path of the repository where the EA model is located.", required=True)
args = parser.parse_args()

pathToEaFile = args.pathToEaFile
# ---------------------
print(f"== Publish MPMS v{__version__} ==")
os.chdir(pathToEaFile)

# Currently, the first found .eapx/.qea file is considered
model = Utils.GetFirstFileInFolder(pathToEaFile)
print (model)
componentMappings = Utils.LoadComponentMappings()
gitBasePath = componentMappings["GitRepoBasePath"]
if model and model != "":
    mpmsfile = Utils.GetFirstFileInFolder(pathToEaFile, "*.mpms")
    # take the guid via substring from e.g "Customer A-46e3f67f-4e87-c98e-129a-049eb063405f"

    fileNameOnly = os.path.basename(mpmsfile)
    componentGuid = fileNameOnly[len(fileNameOnly)-41:len(fileNameOnly)-5]

    # Clear the read-only flag of main mpms files
    Utils.ClearReadOnlyFlag(mpmsfile)

    # update the dependent mpms files to local repo
    rfile = open(mpmsfile, encoding="utf8")
    lines = rfile.readlines()
    for line in lines:
        if line.startswith("//{"):
            substring = line[2: len(line) - 1]
            dependency_list = Utils.get_dependency_mpms(substring)
            for filename in dependency_list:
                print(f" + {filename}")
                gitstatus = Utils.check_local_repostatus(componentMappings, gitBasePath, filename)
                if gitstatus and gitstatus[0]:
                    commit_behind = gitstatus[0][0]
                    commit_ahead = gitstatus[0][1]
                    if not all([True if status == 0 else False for status in [commit_behind,  commit_ahead]]):
                        raise Exception(f"Imported Branch '{gitstatus[1]}' is not upto date for - {gitstatus[-1]}")
    rfile.close()

    print(f"Exporting Components: {componentGuid}")
    publishReturnCode = Utils.LemonTreePublishComponents(model, componentGuid, pathToEaFile)
    print(f"Return code of publish: {publishReturnCode}")
else:
    print("No model file found in the given folder. Aborting MPMS Publish.")
