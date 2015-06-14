from __future__ import print_function
from distutils.command.build import build
from distutils.command.install import install
from distutils.core import setup, Command
from subprocess import check_call, check_output
from urllib2 import urlopen
from zipfile import ZipFile
from StringIO import StringIO

import logging
import os
import re
import shutil
import sys
import time

logger = logging.getLogger("pythran")
logger.addHandler(logging.StreamHandler())

execfile(os.path.join('pythran', 'version.py'))


def _exclude_current_dir_from_import():
    """ Prevents Python loading from current directory, so that
    `import pythran` lookup the PYTHONPATH.

    Returns current_dir

    """
    current_dir = os.path.dirname(os.path.abspath(__file__))
    sys.path = filter(lambda p: p != current_dir, sys.path)
    return current_dir


class BuildWithPly(build):

    """
    Set up Pythran dependencies.

    * generate parsetab file for ply module
    * install nt2
    * install boost.simd
    """

    def build_ply(self):
        """
        Generate parsing file for the Pythran grammar.

        Yacc have to generate a parsetab.py file to quicken futher parsing.
        This file have to be generated before the first use as it is generated
        in the pythran package so it could be a root folder.

        Also, it has to be generated from the "in build" module as we don't
        want create a parsetab file for the "already installed" module.
        """
        sys.path.insert(0, os.path.abspath(self.build_lib))
        from pythran.spec import SpecParser
        SpecParser()  # this forces the generation of the parsetab file
        sys.path.pop(0)

    def patch_nt2(self, nt2_path, version):
        """
        NT2 version gets override by pythran's git version if any
        So force it here...
        """
        cmakelists = os.path.join(nt2_path, 'CMakeLists.txt')
        with open(cmakelists, 'r') as cm:
            print("patching nt2 version in" + cmakelists)
            data = cm.read()
            data = re.sub(r'(nt2_parse_version\()',
                          r'set(NT2_VERSION_STRING "{}")\n\1'.format(version),
                          data)
        with open(cmakelists, 'w') as cm:
            cm.write(data)

        with open(os.path.join(nt2_path, 'tagname'), 'w') as tn:
            tn.write(version)

    def build_nt2(self):
        """ Install NT2 from the github-generated archive. """
        nt2_dir = 'nt2'
        nt2_version = '1.2.3-pythran'  # fake!
        cwd = os.getcwd()
        nt2_src_dir = os.path.join(cwd, self.build_temp, nt2_dir + '_src')
        if not os.path.isdir(nt2_src_dir):
            print('nt2 archive needed, downloading it')
            url = 'https://github.com/pbrunet/nt2/archive/gemv_release.zip'
            location = urlopen(url)
            http_code_prefix = location.getcode() / 100
            assert http_code_prefix not in [4, 5], "Failed to download nt2."
            zipfile = ZipFile(StringIO(location.read()))
            zipfile.extractall(self.build_temp)
            extracted = os.path.dirname(zipfile.namelist()[0])
            shutil.move(os.path.join(self.build_temp, extracted), nt2_src_dir)
            self.patch_nt2(nt2_src_dir, nt2_version)
            assert os.path.isdir(nt2_src_dir), "download & unzip ok"

        nt2_build_dir = os.path.join(self.build_temp, nt2_dir)
        if not os.path.isdir(nt2_build_dir):
            os.makedirs(nt2_build_dir)

        if not os.path.exists(os.path.join(nt2_build_dir, 'doc')):
            print('nt2 not configured, configuring it')
            # remove any remaining artifacts
            shutil.rmtree(nt2_build_dir, True)
            os.makedirs(nt2_build_dir)

            os.chdir(nt2_build_dir)
            build_cmd = ['cmake',
                         nt2_src_dir,
                         '-DNT2_VERSION_STRING={}'.format(nt2_version),
                         '-DCMAKE_INSTALL_PREFIX=.']
            try:
                check_call(build_cmd)
            except Exception:
                print("configure failed upon: " + " " .join(build_cmd))
                raise
            os.chdir(cwd)

        print('Compile and install nt2')
        check_output(['cmake', '--build', nt2_build_dir,
                      '--target', 'install'])
        for d in ('nt2', 'boost'):
            src = os.path.join(nt2_build_dir, 'include', d)

            # copy to the build tree
            target = os.path.join(self.build_lib, 'pythran', d)
            shutil.rmtree(target, True)
            shutil.copytree(src, target)

            # copy them to the source tree too, needed for sdist
            target = os.path.join('pythran', d)
            shutil.rmtree(target, True)
            shutil.copytree(src, target)

    def run(self, *args, **kwargs):
        # regular build done by parent class
        build.run(self, *args, **kwargs)
        if not self.dry_run:  # compatibility with the parent options
            self.build_nt2()
            self.build_ply()


class TestCommand(Command):

    """Scan the test directory for any tests, and run them."""

    description = 'run the test suite for the package'
    user_options = [('failfast', None, 'Stop upon first fail'),
                    ('cov', None, 'Perform coverage analysis'),
                    ('num-threads=', None,
                     'Number of threads to execute tests')]

    def initialize_options(self):
        """ Initialize default value to command line arguments. """
        self.failfast = False
        self.cov = False
        import multiprocessing
        self.num_threads = multiprocessing.cpu_count()

    def finalize_options(self):
        """ Check arguments validity. """
        assert self.num_threads > 0, "Not enough threads for tests."

    def run(self):
        """
        Run tests using the PYTHONPATH path to load pythran.

        It also check third party library installation before running tests.
        """
        # Do not include current directory, validate using installed pythran
        current_dir = _exclude_current_dir_from_import()
        where = os.path.join(current_dir, 'pythran')

        from pythran import test_compile
        test_compile()

        import pytest
        args = [where]

        # try a parallel run
        try:
            import xdist
            args = ["-n", str(self.num_threads)] + args
        except ImportError:
            print('W: Skipping parallel run, pytest_xdist not found')

        # try a parallel run
        try:
            import pytest_pep8
            args = ["--pep8"] + args
        except ImportError:
            print('W: Skipping pep8 checks, pytest_pep8 not found')

        if self.failfast:
            args.insert(0, '-x')

        if self.cov:
            try:
                # Avoid loading unused module
                __import__('imp').find_module('pytest_cov')
                args = ["--cov-report", "html",
                        "--cov-report", "annotate",
                        "--cov", "pythran"] + args
            except ImportError:
                print('W: Skipping coverage analysis, pytest_cov not found')
        if pytest.main(args) == 0:
            print('\\_o<')


class BenchmarkCommand(Command):

    """Scan the test directory for any runnable test, and benchmark them."""

    default_nb_iter = 30
    modes = ("cpython", "pythran", "pythran+omp")
    description = 'run the benchmark suite for the package'
    user_options = [
        ('nb-iter=', None,
         'number of times the benchmark is run'
         '(default={0})'.format(default_nb_iter)),
        ('mode=', None,
         'mode to use ' + str(modes))
    ]

    runas_marker = '#bench '

    def __init__(self, *args, **kwargs):
        Command.__init__(self, *args, **kwargs)

    def initialize_options(self):
        self.nb_iter = BenchmarkCommand.default_nb_iter
        self.parallel = False
        self.mode = "pythran"

    def finalize_options(self):
        self.nb_iter = int(self.nb_iter)
        if self.mode not in BenchmarkCommand.modes:
            raise RuntimeError("Unknown mode : '{}'".format(self.mode))

    def run(self):
        import glob
        import timeit
        from pythran import test_compile, compile_pythranfile
        import random
        import numpy

        # Do not include current directory, validate using installed pythran
        current_dir = _exclude_current_dir_from_import()
        os.chdir("pythran/tests")
        where = os.path.join(current_dir, 'pythran', 'tests', 'cases')

        test_compile()

        candidates = glob.glob(os.path.join(where, '*.py'))
        sys.path.append(where)
        random.shuffle(candidates)
        for candidate in candidates:
            with file(candidate) as content:
                runas = [line for line in content.readlines()
                         if line.startswith(BenchmarkCommand.runas_marker)]
                if runas:
                    modname, _ = os.path.splitext(os.path.basename(candidate))
                    runas_commands = runas[0].replace(
                        BenchmarkCommand.runas_marker, '').split(";")
                    runas_context = ";".join(["import {0}".format(
                        modname)] + runas_commands[:-1])
                    runas_command = modname + '.' + runas_commands[-1]

                    # cleaning
                    sopath = os.path.splitext(candidate)[0] + ".so"
                    if os.path.exists(sopath):
                        os.remove(sopath)

                    ti = timeit.Timer(runas_command, runas_context)

                    print(modname + ' running ...')

                    # pythran part
                    if self.mode.startswith('pythran'):
                        cxxflags = ["-O2", "-DNDEBUG", "-DUSE_BOOST_SIMD",
                                    "-march=native"]
                        if self.mode == "pythran+omp":
                            cxxflags.append("-fopenmp")
                        begin = time.time()
                        compile_pythranfile(candidate,
                                            cxxflags=cxxflags)
                        print('Compilation in : ', (time.time() - begin))

                    sys.stdout.flush()
                    timing = numpy.array(ti.repeat(self.nb_iter, number=1))
                    print('median :', numpy.median(timing))
                    print('min :', numpy.min(timing))
                    print('max :', numpy.max(timing))
                    print('std :', numpy.std(timing))
                    del sys.modules[modname]
                else:
                    print('* Skip ', candidate, ', no ', end='')
                    print(BenchmarkCommand.runas_marker, ' directive')


# Cannot use glob here, as the files may not be generated yet
nt2_headers = (['nt2/' + '*/' * i + '*.hpp' for i in range(1, 20)] +
               ['boost/' + '*/' * i + '*.hpp' for i in range(1, 20)])
pythonic_headers = ['*/' * i + '*.hpp' for i in range(9)] + ['patch/*']

setup(name='pythran',
      version=__version__,
      description=__descr__,
      author='Serge Guelton',
      author_email='serge.guelton@telecom-bretagne.eu',
      url=__url__,
      packages=['pythran', 'pythran.analyses', 'pythran.transformations',
                'pythran.optimizations', 'omp', 'pythran/pythonic',
                'pythran.types'],
      package_data={'pythran': ['pythran*.cfg'] + nt2_headers,
                    'pythran/pythonic': pythonic_headers},
      scripts=['scripts/pythran', 'scripts/pythran-config'],
      classifiers=[
          'Development Status :: 4 - Beta',
          'Environment :: Console',
          'Intended Audience :: Developers',
          'License :: OSI Approved :: BSD License',
          'Natural Language :: English',
          'Operating System :: POSIX :: Linux',
          'Programming Language :: Python :: 2.7',
          'Programming Language :: Python :: Implementation :: CPython',
          'Programming Language :: C++',
          'Topic :: Software Development :: Code Generators'
      ],
      license="BSD 3-Clause",
      cmdclass={'build': BuildWithPly,
                'test': TestCommand,
                'bench': BenchmarkCommand}
      )
