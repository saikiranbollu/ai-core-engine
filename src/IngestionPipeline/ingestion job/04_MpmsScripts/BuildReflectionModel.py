#######################################################
#
# PublishMpmsFileFromEaModel.py
# Python implementation to build the reflection model
# Created on:      12-feb-2022
# Original author: dashora
#
#######################################################

# Includes
import argparse
import os
import shutil
import getpass
import Utils
# --------

# File version
__version__ = "2.2.0"

# argument definition
parser = argparse.ArgumentParser(description="Build Reflection Model Script")
parser.add_argument("-f", "--reflectionModelFullPath", help="Path of the reflection model.")
parser.add_argument("-r", "--directory", help="Path of the git repo.")
parser.add_argument("-b", "--branch", nargs='+', help="Git branch.")
parser.add_argument(
    "--jenkins",
    action="store_true",
    help="If set to true, Jenkins authentication is used. Local users should not pass this variable",
    required=False,
)
parser.add_argument("--username", default="", required=False)
parser.add_argument("--password", default="", required=False)
args = parser.parse_args()

reflectionModelFullPath = args.reflectionModelFullPath
directory = args.directory
branch = args.branch
jenkins = args.jenkins
username = args.username
password = args.password
# ---------------------
mpmsworkspace = r"C:/temp/mpms-workspace"
print(f"== Build Reflection Model v{__version__} ==")
print(f"Creating temporary workspace for scripts: '{mpmsworkspace}'")
Utils.CreateMpmsWorkspace(mpmsworkspace)

# if the reflection model with that name doesn't exist yet, create a new one
if not os.path.isfile(reflectionModelFullPath):
    print(f"Given path '{reflectionModelFullPath}' does not exist! A new Reflection Model will be created.")

    # replacing slashes in branch name since this can be mistaken as a path
    reflectionModelFullPath = reflectionModelFullPath.replace("/", "-")
    shutil.copy(Utils.emptyModel, reflectionModelFullPath)

    componentMappings = Utils.LoadComponentMappings()
    # download all files from BitBucket
    if not jenkins:
        usr = getpass.getuser()
        pwd = getpass.getpass(prompt="Credential required to download file from BitBucket. Enter password: ")
    else:
        usr = username
        pwd = password

    os.chdir(mpmsworkspace)  # change to mpms workspace as curl would download to current directory
    for mapping in componentMappings["Components"]:
        mpmsFileName = mapping["Name"]
        for git_branch in branch:
            downloadSuccessful = Utils.DownloadFileFromBitBucket(mpmsworkspace, mpmsFileName, usr, pwd, git_branch)
            if not downloadSuccessful:
                print(
                    f"File {mpmsFileName} could not be downloaded or doesn't exist in the branch '{branch}'! Placeholders will be imported..."
                )
            else:
                break
    os.chdir(directory)  # switch back to user provided directory


else:
    # Copy the files to mpms workspace
    print(f"Importing the following mpms file(s) into Reflection Model '{reflectionModelFullPath}' ::")
    for file in Utils.GetFilesInFolder(directory, "*.mpms"):
        print(f" - {file}")
        shutil.copy(file, mpmsworkspace)

# Clear the read-only flag of mpms files
fileList = Utils.GetFilesInFolder(mpmsworkspace, "*.mpms")
for file in fileList:
    Utils.ClearReadOnlyFlag(file)

# Import mpms files to EAP/QEA
if len(fileList) != 0:
    Utils.LemonTreeImportMpmsFiles(reflectionModelFullPath, f"{mpmsworkspace}/*.mpms")

    # Update the legend props to avoid overlap
    try:
        Utils.FixDiagramLegendProps(reflectionModelFullPath)
    except Exception as err:
        print(err)

    # Compact and Repair the Reflection model
    # try:
    #     Utils.LockNCompactReflectionModel(reflectionModelFullPath, mpmsworkspace)
    # except Exception as err:
    #     print(err)

    # Lock all packages
    try:
        Utils.LockPackagesRecursively(reflectionModelFullPath)
    except Exception as err:
        print(err)
else:
    print("- NONE.\nNothing to import. No changes made to the reflection model!")

# Clean the workspace before exit
Utils.CreateMpmsWorkspace(mpmsworkspace)
