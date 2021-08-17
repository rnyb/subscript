""" The fmu_copy_revision script.

Purpose is fast and secure copy for fmu revisions using rsync as engine,
with xargs to speed up multithreading.
"""
import argparse
import getpass
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time
from multiprocessing import cpu_count
from os.path import join

import subscript

logger = subscript.getLogger(__name__)

DESCRIPTION = """This is a simple interactive script for copying a FMU revision folder
with features:
    1. Selective copy, i.e. avoid data that can be regenerated
    2. Speed up copying by multithreading
    3. Retain correct file dates and user permissions

Usage:
    fmu_copy_revision  (for menu based input)

     * or *

    fmu_copy_revision --source 21.0.0 --target some --profile 3 --threads 6 --cleanup

     * or *

    fmu_copy_revision --source 21.0.0  (...other options are defaulted)

"""

USERMENU = """\

By default some file types and directories will be skipped. Here are some profiles:

1. Copy everything

2. Copy everything, except:
    * Files and folders containing 'backup' in name
    * Directories and files with name 'users'
    * Directories and files with name 'attic'
    * Directories and files with names '.git' or '.svn'
    * Files ending with ~
    * Empty folders (except those listed above) will be kept

3. Copy everything, except:
    * Files and folders containing 'backup' and in name
    * Directories and files with name 'users'
    * Directories and files with name 'attic'
    * Directories and files with names '.git' or '.svn'
    * Files ending with ~
    * The following folders under ert/ (if they exist):
        - output
        - ert/*/storage, including ert/storage (for backw compat.)
    * The following folders or files under rms/ (if they exist):
        - input/seismic, model/*.log
    * The following files under rms/ (if they exist):
        - Files under output folders (folders will be kept!)
    * The following files and folders under spotfire/:
        - input/*.csv, input/*/.csv model/*.dxp, model/*/*.dxp
    * The following folders under share/:
        - results
        - templates
    * Empty folders (at destination) except those listed above will kept

4. As profile 3, but all empty folder (at destination) will removed.
    This the DEFAULT profile!

5. As profile 3, but keeps more data:
    * Folder rms/output will be copied (~old behaviour)
    * Folders share/results and share/templates will be copied.

6. Only copy the <coviz> folder (if present), which shall be under
    <revision>/share/coviz:
    * Symbolic links will be kept, if possible

9. Make your own filter rules in a named file. For syntax, see e.g.
    https://www.tutorialspoint.com/unix_commands/rsync.htm
"""


# FILTER* are per file filters, used in the second rsync ommand in the shell file below.
# DIRFILTER* are per folder, used if DIRTREE == 1 or 2 in the shell script below.

FILTER1 = """
+ **
"""

FILTER2 = """
- backup/**
- users/**
- attic/**
- .git/**
- *.git
- *.svn
- *~
"""

DIRFILTER2 = """
- backup/
- users/
- attic/
- .git/
+ */
- *
"""

FILTER5_ADD = """
+ rms/output/**
+ share/results/**
+ share/templates/**
"""

FILTER3_ADD = """
- ert/output/**
- ert/storage/**
- ert/output/storage/**
- input/seismic/**
- rms/model/*.log
- rms/output/**
- spotfire/**/*.csv
- spotfire/**/*.dxp
- share/results/**
- share/templates/**
"""

DIRFILTER3 = """
- backup/
- users/
- attic/
- .git/
- ert/output/
- ert/storage/
- ert/**/storage/
- rms/output
- rms/input/seismic/
- share/results/
- share/templates/
+ */
- *
"""

DIRFILTER5 = """
- backup/
- users/
- attic/
- .git/
- ert/output/
- ert/storage/
- ert/**/storage/
- rms/input/seismic/
+ */
- *
"""

DIRFILTERX = """
+ */
- *
"""

FILTER5 = FILTER2 + FILTER3_ADD + FILTER5_ADD
FILTER3 = FILTER2 + FILTER3_ADD

FILTER6 = """
+ share/coviz/**
- *
"""


SHELLSCRIPT = """\
#!/usr/bin/sh

# SETUP OPTIONS

SRCDIR="$1"  # a relative path
DESTDIR="$2"  # must be an absolute path!
FILTERFILE="$3"
THREADS=$4
RSYNCARGS="$5"
DIRTREE=$6  # if 1 first copy folder tree, if 2 do it afterwards with dirfilterfile
DIRFILTERFILE="$7"

PWD=$(pwd)

start=`date +%s.%N`

cd $SRCDIR

echo " ** Target folder is $DESTDIR"
mkdir -p $DESTDIR

echo " ** Sync folders and files!"

if [ $DIRTREE -eq 1 ]; then
    echo " ** Sync all folders first..."  # this is usually fast
    rsync -a -f"+ */" -f"- *" . $DESTDIR
fi

echo " ** Sync files using multiple threads..."
find -L . -type f | xargs -n1 -P$THREADS -I% \
    rsync $RSYNCARGS -f"merge $FILTERFILE" % $DESTDIR

if [ $DIRTREE -eq 2 ]; then
    echo " ** Sync all folders (also empty) except some..."
    rsync -a -f"merge $DIRFILTERFILE" . $DESTDIR
fi

end=`date +%s.%N`

runtime=$( echo "$end - $start" | bc -l )

echo " ** Compute runtime..."

echo $runtime
cd $PWD

"""


def get_parser() -> argparse.ArgumentParser:
    """Setup parser."""

    usetext = "fmu_copy_revision <commandline> OR interactive"
    parser = argparse.ArgumentParser(
        description=DESCRIPTION,
        formatter_class=argparse.RawTextHelpFormatter,
        usage=usetext,
    )

    parser.add_argument("--dryrun", action="store_true", help="Run dry run for testing")
    parser.add_argument(
        "--all, -all, -a", action="store_true", dest="all", help="List all folders"
    )
    parser.add_argument(
        "--verbose, --verbosity, -v",
        action="store_true",
        dest="verbosity",
        help="Enable logging (messages) for debugging",
    )
    # add group for mutually exclusive arguments:
    pgroup = parser.add_mutually_exclusive_group()
    pgroup.add_argument(
        "--cleanup",
        action="store_true",
        dest="cleanup",
        help="Remove (cleanup) if target already exists, default is False.",
    )
    pgroup.add_argument(
        "--merge",
        action="store_true",
        dest="merge",
        help="Try a rsync merge if target already exists, default is False. Note this "
        "operation is currently somewhat experimental. Cannot be combined with "
        "--cleanup",
    )
    parser.add_argument(
        "--skipestimate, --skip",
        "-s",
        action="store_true",
        dest="skipestimate",
        help="If present, skip estimation of current revision size.",
    )
    parser.add_argument("--source", dest="source", type=str, help="Add source folder")
    parser.add_argument("--target", dest="target", type=str, help="Add target folder")
    parser.add_argument(
        "--profile",
        dest="profile",
        type=str,
        default="4",
        help="profile for copy profile to use, default is 3",
    )
    parser.add_argument(
        "--threads",
        dest="threads",
        type=int,
        default=99,  # 99 for automatic
        help="Number of threads, default is computed automatically",
    )

    parser.add_argument(
        "--version",
        action="version",
        version="%(prog)s (subscript version " + subscript.__version__ + ")",
    )

    logger.info("Parsing commandline")
    return parser


class CopyFMU:
    """Class for copying a FMU revision."""

    def __init__(self):
        """Instantiate object."""
        self.args = None
        self.folders = []
        self.source = None
        self.default_target = None
        self.target = None
        self.nthreads = None
        self.profile = 3
        self.filter = ""
        self.dirfilter = ""
        self.batch = False
        self.keepfolders = 0

    def do_parse_args(self, args):
        """Parse command line arguments."""

        if args is None:
            args = sys.argv[1:]

        parser = get_parser()
        self.args = parser.parse_args(args)

    def check_folders(self):
        """Check if potential fmu folders are present or list all if --all."""

        current = pathlib.Path(".")
        folders = [file for file in current.iterdir() if file.is_dir()]
        result = []
        for folder in folders:
            fname = folder.name
            if not self.args.all:
                if fname.startswith(("r", "1", "2", "3")):
                    result.append(fname)
            else:
                result.append(fname)

        if result:
            result = sorted(result)

        if not result:
            print(
                "No valid folders to list. Are you in the correct folder "
                "above your revisions? Or consider --all option to list all folders."
            )
            sys.exit()

        self.folders = result

    def menu_source_folder(self):
        """Print an interactive menu to the user for which folder."""
        print("Choices:\n")

        default = 0
        for inum, res in enumerate(self.folders):
            print(f"{inum + 1:4d}:  {res}")
            default = inum + 1  # take last as default
        try:
            select = int(input(f"\nChoose number, default is {default}: ") or default)
        except ValueError:
            print("Selection is not a number")
            sys.exit()

        if select in range(1, len(self.folders) + 1):
            usefolder = self.folders[select - 1]
            print(f"Selection <{select}> seems valid, folder to use is <{usefolder}>")
        else:
            print("Invalid selection!")
            sys.exit()

        self.source = usefolder

    def construct_default_target(self):
        """Validate source and construct default target from source path."""

        logger.info("Source is %s", self.source)

        sourcepath = pathlib.Path(self.source)
        sourcenode = sourcepath.name

        if not sourcepath.exists():
            raise ValueError("Input folder does not exist!")

        today = time.strftime("%Y%m%d")
        user = getpass.getuser()
        logger.info("User and today: %s %s", user, today)

        userpath = sourcepath.parent / "users"
        if not userpath.exists():
            userpath.mkdir(parents=False, exist_ok=True)
            logger.info("Made folder: users")

        xsource = sourcenode + "_" + today
        logger.info("Userpath is %s", userpath)
        self.default_target = pathlib.Path(userpath) / user / sourcenode / xsource
        logger.info("Default target is %s", self.default_target.resolve())

    def construct_target(self, proposal):
        """Final target as abs path string, and evaluate cleanup or merge."""
        target = pathlib.Path(proposal)
        self.target = str(target.absolute())
        print(f"Selected target is <{self.target}>")

        if self.target == str(pathlib.Path(self.source).absolute()):
            raise RuntimeError("You cannot have same target as source!")

        if target.is_dir():
            print(f"Target is already present: <{self.target}>")
            if self.args.cleanup:
                print("Doing cleanup of current target...")
                shutil.rmtree(self.target)
            elif self.args.merge:
                print("Doing merge copy of current target...")
            else:
                print(
                    "Current target exists but neither --cleanup or --merge is "
                    "applied on command line. So have to exit hard!\nSTOP!\n"
                )
                sys.exit()

    def menu_target_folder(self):
        """Print an interactive menu to the user for target."""

        self.construct_default_target()
        dft = self.default_target
        propose = input(f"Choose output folder (default is <{dft}>: ") or dft
        self.construct_target(propose)

    def check_rms_lockfile(self):
        """Check if RMS project has an active lockfile if interactive mode."""
        lockfiles = pathlib.Path(self.source).glob("rms/model/*/project_lock_file")

        if len(list(lockfiles)) > 0:
            print(
                "Warning, it seems that one or more RMS projects have a lock file "
                "and may perhaps be in a saving process..."
            )
            for lockfile in lockfiles:
                print(f"<{lockfile}> owner of lockfile is {lockfile.owner()}")

            if not self.batch:
                answer = (
                    input("Continue anyway? (default is 'Yes' if you press enter): ")
                    or "Y"
                )
                if not answer.startswith(("y", "Y")):
                    print("Stopped by user")
                    sys.exit()

            print("Will continue...")

    def check_disk_space(self):
        """Checking diskspace."""
        print("Checking disk space at current partition...")
        total, used, free = shutil.disk_usage(".")
        print("  Total: %d G" % (total // (2 ** 30)))
        print("  Used:  %d G" % (used // (2 ** 30)))
        print("  Free:  %d G" % (free // (2 ** 30)))

        if self.args.skipestimate:
            print("  Skip estimation of current revision size!")
            return

        print(f"  Estimate size of current revision <{self.source}> ...")

        freekbytes = free // 1024

        def _get_size(path: str) -> int:
            sum = 0
            for p in pathlib.Path(path).rglob("*"):
                if not p.is_symlink():
                    sum += p.stat().st_size

            return sum

        def _filesize(size: float) -> str:
            for unit in ("B", "K", "M", "G"):
                if size < 1024:
                    break
                size /= 1024
            return f"{size:.1f} {unit}"

        fsize = _get_size(self.source)
        print(f"\n  Size of existing revision is: {_filesize(fsize)}\n")

        sourcekbytes = fsize // 1024
        if sourcekbytes > freekbytes:
            print("Not enough space left for copying! STOP!")
            sys.exit()

        time.sleep(1)

    def show_possible_profiles_copy(self):
        """Show a menu for possible profiles for copy/rsync."""

        print(USERMENU)

        default = "4"
        self.profile = int(input(f"Choose (default is {default}): ") or default)

        if self.profile == 9:
            ffile = input("Choose rsync filter file: ")
            with open(ffile, "r") as stream:
                self.filter = stream.read()

    def define_filterpattern(self):
        """Define filterpattern pattern based on menu choice or command line input."""

        filterpattern = ""
        dirfilterpattern = ""
        self.keepfolders = 0

        if self.profile == 1:
            filterpattern = FILTER1
            self.keepfolders = 1
            dirfilterpattern = DIRFILTERX
        elif self.profile == 2:
            filterpattern = FILTER2
            self.keepfolders = 1
            dirfilterpattern = DIRFILTER2
        elif self.profile == 3:
            filterpattern = FILTER3
            self.keepfolders = 2
            dirfilterpattern = DIRFILTER3
        elif self.profile == 4:
            filterpattern = FILTER3
            self.keepfolders = 0
            dirfilterpattern = DIRFILTERX
        elif self.profile == 5:
            filterpattern = FILTER5
            self.keepfolders = 2
            dirfilterpattern = DIRFILTER5
        elif self.profile == 6:
            filterpattern = FILTER6
            self.keepfolders = 0
            dirfilterpattern = DIRFILTERX

        if self.profile != 9:  # already stored if profile is 9
            self.filter = filterpattern
            self.dirfilter = dirfilterpattern

    def do_rsyncing(self):
        """Do the actual rsync job using a shell script made temporary."""

        logger.info("Source is %s", self.source)
        logger.info("Target is %s", self.target)
        logger.info("Script to execute is %s", SHELLSCRIPT)

        # write shellscript and filterpattern file to a temp folder
        tdir = tempfile.TemporaryDirectory()
        logger.info("The tmpdir is %s", tdir.name)
        scriptname = join(tdir.name, "rsync.sh")
        filterpatternname = join(tdir.name, "filterpattern.txt")
        dirfilterpatternname = join(tdir.name, "dirfilterpattern.txt")

        with open(scriptname, "w") as stream:
            stream.write(SHELLSCRIPT)

        with open(filterpatternname, "w") as stream:
            stream.write(self.filter)

        with open(dirfilterpatternname, "w") as stream:
            stream.write(self.dirfilter)

        logger.info("FILE FILTER FILE: %s", filterpatternname)
        logger.info("DIR FILTER FILE: %s", dirfilterpatternname)
        logger.debug("FILE FILTER IS\n: %s\n", self.filter)
        logger.debug("DIR FILTER IS\n: %s\n", self.dirfilter)

        self.nthreads = self.args.threads
        if self.nthreads == 99:
            # as default, leave one CPU free for other use
            self.nthreads = cpu_count() - 1 if cpu_count() > 1 else 1

        print(f"Doing copy using {self.nthreads} CPU threads, please wait...")

        # the -R (--relative) is crucial for making filter profiles work!
        rsyncargs = "-a -R --delete"

        if self.args.dryrun:
            rsyncargs += " --dry-run -v"

        if self.args.verbosity:
            rsyncargs += " -v"

        # execute rsync, some explanations:
        # filterpatternname: is the file to do per file filtering
        # rsyncargs: see above
        # self.keepfolders will be:
        #   - 0 to delete all folders that are empty after copy/filtering
        #   - 1 to keep all folders, even the empty ones
        #   - 2 to keep all folders except those in dirfilterpatternname

        command = [
            "sh",
            scriptname,
            self.source,
            self.target,
            filterpatternname,
            str(self.nthreads),
            str(rsyncargs),
            str(self.keepfolders),
            dirfilterpatternname,
        ]
        logger.info(" ".join(command))

        if sys.version_info[1] >= 7:
            process = subprocess.run(
                command, check=True, shell=False, capture_output=True
            )
        else:
            # capture_output is not supported in 3.6 https://www.py4u.net/discuss/195326
            process = subprocess.run(
                command,
                check=True,
                shell=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

        if process.returncode != 0:
            print(f"Process returncode: {process.returncode}")
            print(process.stderr.decode())
        stdout = process.stdout.decode().splitlines()

        output = "\n".join(stdout[0:-2])
        print(output)
        timing = float(stdout[-1])
        timing = time.strftime("%H hours %M minutes %S seconds", time.gmtime(timing))
        print(
            f"\n ** The rsync process took {timing}, using "
            f"{self.nthreads} threads **\n"
        )


def main(args=None) -> None:
    """Entry point for command line."""

    runner = CopyFMU()
    runner.do_parse_args(args)

    if runner.args.verbosity:
        logger.setLevel("DEBUG")

    if not runner.args.source:
        # interactive menues
        runner.check_folders()
        runner.menu_source_folder()
        runner.menu_target_folder()
        runner.check_rms_lockfile()
        runner.check_disk_space()
        runner.show_possible_profiles_copy()
        runner.define_filterpattern()
        runner.do_rsyncing()
    else:
        print("Command line mode!")
        runner.profile = int(runner.args.profile)
        runner.source = runner.args.source
        runner.batch = True

        if not runner.args.target:
            runner.construct_default_target()
            proposal = runner.default_target
        else:
            proposal = pathlib.Path(runner.args.target)
        runner.construct_target(proposal)
        runner.check_disk_space()
        runner.define_filterpattern()
        print(
            f"Using source <{runner.source}>, target <{runner.target}> with "
            f"profile <{runner.profile}> ..."
        )
        runner.do_rsyncing()


if __name__ == "__main__":
    main()
