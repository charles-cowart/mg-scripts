from os.path import join
from .Job import Job, KISSLoader
from .PipelineError import JobFailedError
import logging
from jinja2 import Environment
from .Pipeline import Pipeline
from .PipelineError import PipelineError
from metapool import load_sample_sheet
from datetime import datetime


logging.basicConfig(level=logging.DEBUG)


class TellReadJob(Job):
    def __init__(self, run_dir, output_path, sample_sheet_path, queue_name,
                 node_count, wall_time_limit, jmem, modules_to_load,
                 qiita_job_id, label, reference_base,
                 reference_map, tmp1_path, sing_script_path, lane,
                 cores_per_task):
        """
        ConvertJob provides a convenient way to run bcl-convert or bcl2fastq
        on a directory BCL files to generate Fastq files.
        :param run_dir: The 'run' directory that contains BCL files.
        :param output_path: Path where all pipeline-generated files live.
        :param sample_sheet_path: The path to a sample-sheet.
        :param queue_name: The name of the Torque queue to use for processing.
        :param node_count: The number of nodes to request.
        :param wall_time_limit: A hard time limit (in min) to bound processing.
        :param jmem: String representing total memory limit for entire job.
        :param modules_to_load: A list of Linux module names to load
        :param qiita_job_id: identify Torque jobs using qiita_job_id
        :param label: None
        :param reference_base: None
        :param reference_map: None
        :param cores_per_task: (Optional) # of CPU cores per node to request.
        """
        super().__init__(run_dir,
                         output_path,
                         'TellReadJob',
                         [],
                         1,
                         modules_to_load=modules_to_load)

        self.sample_sheet_path = sample_sheet_path
        self._file_check(self.sample_sheet_path)
        metadata = self._process_sample_sheet()
        self.sample_ids = metadata['sample_ids']
        self.queue_name = queue_name
        self.node_count = node_count
        self.wall_time_limit = wall_time_limit
        self.cores_per_task = cores_per_task

        self.reference_base = reference_base
        self.reference_map = reference_map

        # raise an Error if jmem is not a valid floating point value.
        self.jmem = str(int(jmem))
        self.qiita_job_id = qiita_job_id
        self.jinja_env = Environment(loader=KISSLoader('templates'))
        self.sing_script_path = sing_script_path
        self.tmp1_path = tmp1_path

        # force self.lane_number to be int. raise an Error if it's not.
        tmp = int(lane)
        if tmp < 1 or tmp > 8:
            raise ValueError(f"'{tmp}' is not a valid lane number")
        self.lane_number = tmp

        # TODO: Need examples of these being not None
        if self.reference_base is not None or self.reference_map is not None:
            tag = 'reference-based'
        else:
            tag = 'reference-free'

        date = datetime.today().strftime('%Y.%m.%d')
        self.job_name = (f"{label}-{tag}-{date}-tellread")

    def run(self, callback=None):
        job_script_path = self._generate_job_script()
        params = ['--parsable',
                  f'-J {self.job_name}',
                  '-c ${sbatch_cores}',
                  '--mem ${sbatch_mem}',
                  '--time ${wall}']

        try:
            self.job_info = self.submit_job(job_script_path,
                                            job_parameters=' '.join(params),
                                            exec_from=None,
                                            callback=callback)

            logging.debug(f'TellReadJob Job Info: {self.job_info}')
        except JobFailedError as e:
            # When a job has failed, parse the logs generated by this specific
            # job to return a more descriptive message to the user.
            # TODO: We need more examples of failed jobs before we can create
            #  a parser for the logs.
            # info = self.parse_logs()
            # prepend just the message component of the Error.
            # info.insert(0, str(e))
            info = str(e)
            raise JobFailedError('\n'.join(info))

        logging.debug(f'TellReadJob {self.job_info["job_id"]} completed')

    def _process_sample_sheet(self):
        sheet = load_sample_sheet(self.sample_sheet_path)

        if not sheet.validate_and_scrub_sample_sheet():
            s = "Sample sheet %s is not valid." % self.sample_sheet_path
            raise PipelineError(s)

        header = sheet.Header
        chemistry = header['chemistry']

        if header['Assay'] not in Pipeline.assay_types:
            s = "Assay value '%s' is not recognized." % header['Assay']
            raise PipelineError(s)

        sample_ids = []
        for sample in sheet.samples:
            sample_ids.append((sample['Sample_ID'], sample['Sample_Project']))

        bioinformatics = sheet.Bioinformatics

        # reorganize the data into a list of dictionaries, one for each row.
        # the ordering of the rows will be preserved in the order of the list.
        lst = bioinformatics.to_dict('records')

        # human-filtering jobs are scoped by project. Each job requires
        # particular knowledge of the project.
        return {'chemistry': chemistry,
                'projects': lst,
                'sample_ids': sample_ids}

    def _generate_job_script(self):
        job_script_path = join(self.output_path, 'tellread_test.sbatch')
        template = self.jinja_env.get_template("tellread.sbatch")

        # generate a comma separated list of sample-ids from the tuples stored
        # in self.sample_ids.

        # NB: the current sample-sheet format used for TellRead doesn't include
        # sample-names and sample-ids, only sample_id. e.g. C501,C502,etc.
        # Hence, when a final sample sheet format is ready, it may be prudent
        # to switch this to pull values from the expected sample-names column
        # instead.
        samples = ','.join([id[0] for id in self.sample_ids])

        # since we haven't included support for reference_map yet, whenever a
        # reference is not included, the mapping against the list of sample_ids
        # is ['NONE', 'NONE', ..., 'NONE'].
        refs = ','.join(['NONE' for _ in self.sample_ids])

        extra = ""

        # if reference_base is added in the future and is defined, extra needs
        # to be f"-f {reference_base}".
        # extra = "-f ${REFBASE}"

        with open(job_script_path, mode="w", encoding="utf-8") as f:
            f.write(template.render({
                "job_name": "tellread",
                "wall_time_limit": self.wall_time_limit,
                "mem_in_gb": self.jmem,
                "node_count": self.node_count,
                "cores_per_task": self.cores_per_task,
                "queue_name": self.queue_name,
                "sing_script_path": self.sing_script_path,
                "tmp_dir": self.tmp1_path,
                "modules_to_load": ' '.join(self.modules_to_load),
                "lane": f"s_{self.lane_number}",
                "output": join(self.output_path, "output"),
                "rundir_path": self.root_dir,
                "samples": samples,
                "refs": refs,
                "extra": extra
            }))

        return job_script_path

    def parse_logs(self):
        raise PipelineError("parse_logs() not implemented for TellReadJob")