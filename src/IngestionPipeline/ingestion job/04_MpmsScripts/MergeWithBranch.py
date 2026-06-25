#######################################################
#
# PublishMpmsFileFromEaModel.py
# Python implementation to generate a predictive diff report of EA model after branch is merged to mainline.
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
parser.add_argument("branchForMerge", help="Name of the branch to merge against.")
parser.add_argument("mergeSessionOutput", help="Path and name of the .ltsfs file of the diff with the previous commit.")
parser.add_argument("--jenkins", help="If set to true, will use Jenkins workspace master for comparison")
args = parser.parse_args()

directory = args.directory
branchForMerge = args.branchForMerge
mergeSessionOutput = args.mergeSessionOutput
jenkins = args.jenkins
# ---------------------

print(f"== MPMS Diff Report (Merge-Diff) Generator v{__version__} ==")
os.chdir(directory)

# get mpms file
mpmsfile = Utils.GetFirstFileInFolder(directory, "*.mpms")
tempNameOfMpms = "temp_willBeRenamed.mpms"
baseCommitName = "baseCommit.mpms"
mergeCommitName = "mergeCommit.mpms"
originalName = os.path.basename(mpmsfile)
print(originalName)
# before getting commits, fix the git config (branch is detached and other branches like develop or remote are not available)
# if this is done, other branches are not checked out and need to be referred to as "origin/branchName"
Utils.GitFixDetachedHead()

# get the commit hashes from git
# proc = subprocess.Popen('git rev-parse head', stdout=subprocess.PIPE)
# currentCommit = proc.stdout.readline()

currentCommit = Utils.RunGitCommandWithResult("git rev-parse head")
mergeBranchCommit = Utils.RunGitCommandWithResult(f"git rev-parse origin/{branchForMerge}")

baseCommit = Utils.RunGitCommandWithResult(f"git merge-base {currentCommit} {mergeBranchCommit}")

print(currentCommit)
print(mergeBranchCommit)
print(baseCommit)

# rename existing files temporarily
os.rename(originalName, tempNameOfMpms)

# checkout merge commit
subprocess.call(f"git checkout -q {mergeBranchCommit} \"{originalName}\"")

# rename merge commit
os.rename(originalName, mergeCommitName)

removeBaseFile=False

# if the two commits are not equal, we have to get the base as well ---> real three-way merge
if mergeBranchCommit != baseCommit:
    # checkout base commit
    subprocess.call(f"git checkout -q {baseCommit} {mpmsfile}")

    # rename base commit
    os.rename(originalName, baseCommitName)

    removeBaseFile=True
else:
    baseCommitName = mergeCommitName

# set main file back to correct name
os.rename(tempNameOfMpms, originalName)

# start LT diff and write session file
mergeReturnCode = Utils.LemonTreeGenerateMergeSession(baseCommitName, mergeCommitName, originalName, mergeSessionOutput)

print(f"Return code of merge: {mergeReturnCode}")

# remove base and merge commit file
os.remove(mergeCommitName)

if removeBaseFile:
    os.remove(baseCommitName)

if mergeReturnCode != 0 :
    sys.exit(mergeReturnCode)
