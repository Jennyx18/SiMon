import os
import os.path
import sys
import time
import logging
import glob

import datetime
import numpy
try:
    import configparser as cp  # Python 3 only
except ImportError:
    import ConfigParser as cp  # Python 2 only
from fnmatch import fnmatch
from daemon import runner
from module_common import SimulationTask

__simon_dir__ = os.path.dirname(os.path.abspath(__file__))


class SiMon(object):
    """
    Main code of Simulation Monitor (SiMon).
    """

    def __init__(self, pidfile=None, stdin='/dev/tty', stdout='/dev/tty', stderr='/dev/tty',
                 mode='interactive', cwd=None, config_file='SiMon.conf'):
        """
        :param pidfile:
        """

        # Only needed in interactive mode
        self.config = self.parse_config_file(config_file)
        if self.config is None:
            print('Error: Configure file SiMon.conf does not exist.')
            sys.exit(-1)
        else:
            try:
                cwd = self.config.get('SiMon', 'Root_dir')
            except cp.NoOptionError:
                print('Item Root_dir is missing in configure file SiMon.conf. SiMon cannot start. Exiting...')
                sys.exit(-1)
        # make sure that cwd is the absolute path
        if not os.path.isabs(cwd):
            cwd = os.path.join(__simon_dir__, cwd)
        if not os.path.isdir(cwd):
            print('Simulation root directory does not exist. Existing...')
            sys.exit(-1)

        self.module_dict = self.register_modules()

        self.selected_inst = []  # A list of the IDs of selected simulation instances
        self.sim_inst_dict = dict()  # the container of all SimulationTask objects (ID to object mapping)
        self.sim_inst_parent_dict = dict()  # given the current path, find out the instance of the parent

        # TODO: create subclass instance according to the config file
        self.sim_tree = SimulationTask(0, 'root', cwd, SimulationTask.STATUS_NEW)
        self.stdin_path = stdin
        self.stdout_path = stdout
        self.stderr_path = stderr
        self.pidfile_path = pidfile
        self.pidfile_timeout = 5
        self.mode = mode
        self.cwd = cwd
        self.inst_id = 0
        self.tcrit = 100
        self.logger = None
        self.max_concurrent_jobs = 2

        if self.config.has_option('SiMon', 'Max_concurrent_jobs'):
            self.max_concurrent_jobs = self.config.getint('SiMon', 'Max_concurrent_jobs')

        os.chdir(cwd)

    @staticmethod
    def id_input(prompt):
        """
        Prompt to the user to input the simulation ID (in the interactive mode)
        """
        confirmed = False
        vec_index_selected = []
        while confirmed is False:
            response = raw_input(prompt)
            fragment = response.split(',')
            for token_i in fragment:
                if '-' in token_i:  # it is a range
                    limits = token_i.split('-')
                    if len(limits) == 2:
                        if int(limits[0].strip()) < int(limits[1].strip()):
                            subrange = range(int(limits[0].strip()), int(limits[1].strip())+1)
                            for j in subrange:
                                vec_index_selected.append(j)
                else:
                    vec_index_selected.append(token_i.strip())
            if raw_input('Your input is \n\t'+str(vec_index_selected)+', confirm? [Y/N] ').lower() == 'y':
                confirmed = True
                return map(int, vec_index_selected)

    @staticmethod
    def parse_config_file(config_file):
        """
        Parse the configure file (SiMon.conf) for starting SiMon. The basic information of Simulation root directory
        must exist in the configure file before SiMon can start. A minimum configure file of SiMon looks like:

        ==============================================
        [SiMon]
        Root_dir: <the_root_dir_of_the_simulation_data>
        ==============================================

        :return: return 0 if succeed, -1 if failed (file not exist, and cannot be created). If the file does not exist
        but a new file with default values is created, the method returns 1.
        """
        conf = cp.ConfigParser()
        if os.path.isfile(config_file):
            conf.read(config_file)
            return conf
        else:
            return None

    @staticmethod
    def register_modules():
        """
        Register modules
        :return: A dict-like mapping between the name of the code and the filename of the module.
        """
        mod_dict = dict()
        module_candidates = glob.glob('module_*.py')
        for mod_name in module_candidates:
            mod = __import__(mod_name.split('.')[0])
            if hasattr(mod, '__simulation__'):
                # it is a valid SiMon module
                mod_dict[mod.__simulation__] = mod_name.split('.')[0]
        return mod_dict

    def traverse_simulation_dir_tree(self, pattern, base_dir, files):
        """
        Traverse the simulation file structure tree (Breadth-first search), until the leaf (i.e. no restart directory)
        or the simulation is not restartable (directory with the 'STOP' file).
        """
        for filename in sorted(files):
            if fnmatch(filename, pattern):
                if os.path.isdir(os.path.join(base_dir, filename)):
                    fullpath = os.path.join(base_dir, filename)
                    self.inst_id += 1
                    id = self.inst_id

                    # Try to determine the simulation code type by reading the config file
                    sim_config = self.parse_config_file(os.path.join(fullpath, 'SiMon.conf'))
                    sim_inst = None
                    if sim_config is not None:
                        try:
                            code_name = sim_config.get('Simulation', 'Code_name')

                            if code_name in self.module_dict:
                                sim_inst_mod = __import__(self.module_dict[code_name])
                                sim_inst = getattr(sim_inst_mod, code_name)(id, filename, fullpath, SimulationTask.STATUS_NEW)
                        except cp.NoOptionError:
                            pass
                    self.sim_inst_dict[id] = sim_inst
                    sim_inst.id = id
                    sim_inst.fulldir = fullpath
                    sim_inst.name = filename

                    # register child to the parent
                    self.sim_inst_parent_dict[base_dir].restarts.append(sim_inst)
                    sim_inst.level = self.sim_inst_parent_dict[base_dir].level + 1
                    # register the node itself in the parent tree
                    self.sim_inst_parent_dict[fullpath] = sim_inst
                    sim_inst.parent_id = self.sim_inst_parent_dict[base_dir].id

                    # Get simulation status
                    sim_inst.sim_get_status()

                    # TODO: add error type detection to sim_inst.sim_check_status()
                    # sim_inst.errortype = self.check_instance_error_type(id)
                    self.sim_inst_dict[sim_inst.parent_id].status = sim_inst.status

                    if sim_inst.t > self.sim_inst_dict[sim_inst.parent_id].t and \
                            not os.path.isfile(os.path.join(sim_inst.fulldir, 'ERROR')):
                        # nominate as restart candidate
                        self.sim_inst_dict[sim_inst.parent_id].cid = sim_inst.id
                        self.sim_inst_dict[sim_inst.parent_id].t_max_extended = sim_inst.t_max_extended

    def build_simulation_tree(self):
        """
        Generate the simulation tree data structure, so that a restarted simulation can trace back
        to its ancestor.

        :return: The method has no return. The result is stored in self.sim_tree.
        :type: None
        """
        os.chdir(self.cwd)
        self.sim_inst_dict = dict()

        self.sim_tree = SimulationTask(0, 'root', self.cwd, SimulationTask.STATUS_NEW)  # initially only the root node
        self.sim_inst_dict[0] = self.sim_tree  # map ID=0 to the root node
        self.sim_inst_parent_dict[self.cwd.strip()] = self.sim_tree  # map the current dir to be the sim tree root
        self.inst_id = 0
        os.path.walk(self.cwd, self.traverse_simulation_dir_tree, '*')

        # Synchronize the status tree (status propagation)
        update_needed = True
        max_iter = 0
        while update_needed and max_iter < 30:
            max_iter += 1
            inst_status_modified = False
            for i in self.sim_inst_dict:
                if i == 0:
                    continue
                inst = self.sim_inst_dict[i]
                if inst.status == SimulationTask.STATUS_RUN or inst.status == SimulationTask.STATUS_DONE:
                    if inst.parent_id > 0 and self.sim_inst_dict[inst.parent_id].status != inst.status:
                        # propagate the status of children (restarted simulation) to parents' status
                        self.sim_inst_dict[inst.parent_id].status = inst.status
                        inst_status_modified = True
            if inst_status_modified is True:
                update_needed = True
            else:
                update_needed = False
        return 0
        # print self.sim_tree

    def print_sim_status_overview(self, sim_id):
        """
        Output an overview of the simulation status in the terminal.

        :return: start and stop time
        :rtype: int
        """
        print(self.sim_inst_dict[sim_id])  # print the root node will cause the whole tree to be printed
        return self.sim_inst_dict[sim_id].t_min, self.sim_inst_dict[sim_id].t_max

    @staticmethod
    def print_help():
        print('Usage: python simon.py start|stop|interactive|help')
        print('\tstart: start the daemon')
        print('\tstop: stop the daemon')
        print('\tinteractive: run in interactive mode (no daemon) [default]')
        print('\thelp: print this help message')

    @staticmethod
    def print_task_selector():
        """
        Prompt a menu to allow the user to select a task.

        :return: current selected task symbol.
        """
        opt = ''
        while opt.lower() not in ['l', 's', 'n', 'r', 'c', 'x', 't', 'd', 'k', 'b', 'p', 'q']:
            sys.stdout.write('\n=======================================\n')
            sys.stdout.write('\tList Instances (L), \n\tSelect Instance (S), '
                             '\n\tNew Run (N), \n\tRestart (R), \n\tCheck status (C), '
                             '\n\tExecute (X), \n\tStop Simulation (T), \n\tDelete Instance (D), \n\tKill Instance (K), '
                             '\n\tBackup Restart File (B), \n\tPost Processing (P), \n\tQuit (Q): \n')
            opt = raw_input('\nPlease choose an action to continue: ').lower()

        return opt

    def task_handler(self, opt):
        """
        Handles the task selection input from the user (in the interactive mode).

        :param opt: The option from user input.
        """

        if opt == 'q':  # quit interactive mode
            sys.exit(0)
        if opt == 'l':  # list all simulations
            self.build_simulation_tree()
            self.print_sim_status_overview(0)
        if opt in ['s', 'n', 'r', 'c', 'x', 't', 'd', 'k', 'b', 'p']:
            if self.mode == 'interactive':
                if self.selected_inst is None or len(self.selected_inst) == 0 or opt == 's':
                    self.selected_inst = self.id_input('Please specify a list of IDs: ')
                    sys.stdout.write('Instances ' + str(self.selected_inst) + ' selected.\n')

        # TODO: use message? to rewrite this part in a smarter way
        if opt == 'n':  # start new simulations
            for sid in self.selected_inst:
                if sid in self.sim_inst_dict:
                    self.sim_inst_dict[sid].sim_start()
                else:
                    print('The selected simulation with ID = %d does not exist. Simulation not started.\n' % sid)
        if opt == 'r':  # restart simulations
            for sid in self.selected_inst:
                if sid in self.sim_inst_dict:
                    self.sim_inst_dict[sid].sim_restart
                else:
                    print('The selected simulation with ID = %d does not exist. Simulation not restarted.\n' % sid)
        if opt == 'c':  # check the recent or current status of the simulation and print it
            for sid in self.selected_inst:
                if sid in self.sim_inst_dict:
                    self.sim_inst_dict[sid].sim_collect_recent_output_message()
                else:
                    print('The selected simulation with ID = %d does not exist. Simulation not restarted.\n' % sid)
        if opt == 'x':  # execute an UNIX shell command in the simulation directory
            print('Executing an UNIX shell command in the selected simulations.')
            shell_command = raw_input('CMD>> ')
            for sid in self.selected_inst:
                if sid in self.sim_inst_dict:
                    self.sim_inst_dict[sid].sim_shell_exec(shell_command=shell_command)
                else:
                    print('The selected simulation with ID = %d does not exist. Cannot execute command.\n' % sid)
        if opt == 't':  # soft-stop the simulation in the ways that supported by the code
            for sid in self.selected_inst:
                if sid in self.sim_inst_dict:
                    self.sim_inst_dict[sid].sim_stop()
                else:
                    print('The selected simulation with ID = %d does not exist. Simulation not stopped.\n' % sid)
        if opt == 'd':  # delete the simulation tree and all its data
            for sid in self.selected_inst:
                if sid in self.sim_inst_dict:
                    self.sim_inst_dict[sid].sim_delete()
                else:
                    print('The selected simulation with ID = %d does not exist. Cannot delete simulation.\n' % sid)
        if opt == 'k':  # kill the UNIX process associate with a simulation task
            for sid in self.selected_inst:
                if sid in self.sim_inst_dict:
                    self.sim_inst_dict[sid].sim_kill()
                else:
                    print('The selected simulation with ID = %d does not exist. Cannot kill simulation.\n' % sid)
        if opt == 'b':  # backup the simulation checkpoint files (for restarting purpose in the future)
            for sid in self.selected_inst:
                if sid in self.sim_inst_dict:
                    self.sim_inst_dict[sid].sim_backup_checkpoint()
                else:
                    print('The selected simulation with ID = %d does not exist. Cannot backup checkpoint.\n' % sid)
        if opt == 'p':  # perform (post)-processing (usually after the simulation is done)
            for sid in self.selected_inst:
                if sid in self.sim_inst_dict:
                    pass
                else:
                    print('The selected simulation with ID = %d does not exist. Cannot perform postprocessing.\n' % sid)

    def auto_scheduler(self):
        """
        The automatic decision maker for the daemon.

        The daemon invokes this method at a fixed period of time. This method checks the
        status of all simulations by traversing to all simulation directories and parsing the
        output files. It subsequently deals with the simulation instance according to the informtion
        gathered.
        """
        os.chdir(self.cwd)
        self.build_simulation_tree()
        schedule_list = []
        # Sort jobs according to priority (niceness)
        sim_niceness_vec = []

        # check how many simulations are running
        concurrent_jobs = 0
        for i in self.sim_inst_dict.keys():
            inst = self.sim_inst_dict[i]
            sim_niceness_vec.append(inst.niceness)
            inst.sim_get_status()  # update its status
            # test if the process is running
            if inst.status == SimulationTask.STATUS_RUN and inst.cid == -1:
                concurrent_jobs += 1

        index_niceness_sorted = numpy.argsort(sim_niceness_vec)
        for ind in index_niceness_sorted:
            if self.sim_inst_dict[ind].status != SimulationTask.STATUS_DONE and self.sim_inst_dict[ind].id > 0:
                schedule_list.append(self.sim_inst_dict[ind])
                print(self.sim_inst_dict[ind].name)

        for sim in schedule_list:
            if sim.id == 0:  # the root group, skip
                continue
            sim.sim_get_status()  # update its status
            print('Checking instance #%d ==> %s [%s]' % (sim.id, sim.name, sim.status))
            if sim.status == SimulationTask.STATUS_RUN:
                sim.sim_backup_checkpoint()
            elif sim.status == SimulationTask.STATUS_STALL:
                sim.sim_kill()
                self.build_simulation_tree()
            elif sim.status == SimulationTask.STATUS_STOP:
                self.logger.warning('STOP detected: '+sim.fulldir+'  '+str(concurrent_jobs))
                # check if there is available slot to restart the simulation
                if concurrent_jobs < self.max_concurrent_jobs and sim.level == 1:
                    # search only top level instance to find the restart candidate
                    # build restart path
                    current_inst = sim
                    while current_inst.cid != -1:
                        current_inst = self.sim_inst_dict[current_inst.cid]
                    # restart the simulation instance at the leaf node
                    print('RESTART: #%d ==> %s' % (current_inst.id, current_inst.fulldir))
                    self.logger.info('RESTART: #%d ==> %s' % (current_inst.id, current_inst.fulldir))
                    current_inst.sim_restart()
                    concurrent_jobs += 1
            elif sim.status == SimulationTask.STATUS_NEW:
                # check if there is available slot to start the simulation
                if concurrent_jobs < self.max_concurrent_jobs:
                    # Start new run
                    sim.sim_start()
                    concurrent_jobs += 1

    def run(self):
        """
        The entry point of this script if it is run with the daemon.
        """
        os.chdir(self.cwd)
        self.build_simulation_tree()
        while True:
            # print('[%s] Auto scheduled' % datetime.datetime.fromtimestamp(time.time()).strftime('%Y-%m-%d %H:%M:%S'))
            self.logger.info('SiMon routine checking...')
            self.auto_scheduler()
            sys.stdout.flush()
            sys.stderr.flush()
            if self.config.has_option('SiMon', 'daemon_sleep_time'):
                time.sleep(self.config.getfloat('SiMon', 'daemon_sleep_time'))
            else:
                time.sleep(180)

    def interactive_mode(self):
        """
        Run SiMon in the interactive mode. In this mode, the user can see an overview of the simulation status from the
        terminal, and control the simulations accordingly.
        :return:
        """
        print os.getcwd()
        os.chdir(self.cwd)
        self.build_simulation_tree()
        self.print_sim_status_overview(0)
        choice = ''
        while choice != 'q':
            choice = SiMon.print_task_selector()
            self.task_handler(choice)

    @staticmethod
    def daemon_mode(simon_dir):
        """
        Run SiMon in the daemon mode.

        In this mode, SiMon will behave as a daemon process. It will scan all simulations periodically, and take measures
        if necessary.
        :return:
        """
        app = SiMon(pidfile=os.path.join(os.getcwd(), 'run_mgr_daemon.pid'),
                    stdout=os.path.join(os.getcwd(), 'SiMon.out.txt'),
                    stderr=os.path.join(os.getcwd(), 'SiMon.err.txt'),
                    cwd=simon_dir,
                    mode='daemon')
        # log system
        app.logger = logging.getLogger("DaemonLog")
        app.logger.setLevel(logging.INFO)
        formatter = logging.Formatter("%(asctime)s - [%(levelname)s] - %(name)s - %(message)s")
        handler = logging.FileHandler(os.path.join(simon_dir, 'SiMon.log'))
        handler.setFormatter(formatter)
        app.logger.addHandler(handler)
        # initialize the daemon runner
        daemon_runner = runner.DaemonRunner(app)
        # This ensures that the logger file handle does not get closed during daemonization
        daemon_runner.daemon_context.files_preserve = [handler.stream]
        daemon_runner.do_action()  # fixed time period of calling run()

if __name__ == "__main__":
    # execute only if run as a script
    if len(sys.argv) == 1:
        print('Running SiMon in the interactive mode...')
        s = SiMon()
        s.interactive_mode()
    elif len(sys.argv) > 1:
        if sys.argv[1] in ['start', 'stop']:
            # python daemon will handle these two arguments
            SiMon.daemon_mode(os.getcwd())
        elif sys.argv[1] in ['interactive', 'i']:
            s = SiMon()
            s.interactive_mode()
        else:
            SiMon.print_help()
            sys.exit(0)
