from aiida.orm.data.structure import StructureData
from aiida.orm.data.parameter import ParameterData
from aiida.orm.data.array import ArrayData
from aiida.orm.data.base import Int, Float, Str, Bool
from aiida.orm.data.singlefile import SinglefileData
from aiida.orm.data.remote import RemoteData
from aiida.orm.code import Code

from aiida.work.workchain import WorkChain, ToContext, Calc, while_
from aiida.work.run import submit

from aiida_cp2k.calculations import Cp2kCalculation

from evalmorbs import EvalmorbsCalculation
from stmimage import StmimageCalculation

import os
import tempfile
import shutil
import numpy as np

class STMWorkChain(WorkChain):

    @classmethod
    def define(cls, spec):
        super(STMWorkChain, cls).define(spec)
        
        spec.input("cp2k_code", valid_type=Code)
        spec.input("structure", valid_type=StructureData)
        spec.input("cell", valid_type=ArrayData)
        spec.input("mgrid_cutoff", valid_type=Int, default=Int(600))
        spec.input("wfn_file_path", valid_type=Str, default=Str(""))
        spec.input("elpa_switch", valid_type=Bool, default=Bool(True))
        
        
        spec.input("eval_orbs_code", valid_type=Code)
        spec.input("eval_orbs_params", valid_type=ParameterData)
        
        spec.input("stm_image_code", valid_type=Code)
        spec.input("stm_image_params", valid_type=ParameterData)
        
        spec.outline(
            cls.run_scf_diag,
            cls.eval_orbs_on_grid,
            cls.make_stm_images,
        )
        
        spec.dynamic_output()
    
    def run_scf_diag(self):
        self.report("Running CP2K diagonalization SCF")

        inputs = self.build_cp2k_inputs(self.inputs.structure,
                                        self.inputs.cell,
                                        self.inputs.cp2k_code,
                                        self.inputs.mgrid_cutoff,
                                        self.inputs.wfn_file_path,
                                        self.inputs.elpa_switch)

        self.report("inputs: "+str(inputs))
        future = submit(Cp2kCalculation.process(), **inputs)
        return ToContext(scf_diag=Calc(future))
   
    def eval_orbs_on_grid(self):
        self.report("Evaluating Kohn-Sham orbitals on grid")
        
        inputs = {}
        inputs['_label'] = "eval_morbs"
        inputs['code'] = self.inputs.eval_orbs_code
        inputs['parameters'] = self.inputs.eval_orbs_params
        inputs['parent_calc_folder'] = self.ctx.scf_diag.out.remote_folder
        inputs['_options'] = {
            "resources": {"num_machines": 2, "num_mpiprocs_per_machine": 6},
            "max_wallclock_seconds": 7200,
        }
        
        self.report("Inputs: " + str(inputs))
        
        future = submit(EvalmorbsCalculation.process(), **inputs)
        return ToContext(eval_morbs=future)
        
        
           
    def make_stm_images(self):
        self.report("Extrapolating wavefunctions and making STM/STS images")
             
        inputs = {}
        inputs['_label'] = "stm_images"
        inputs['code'] = self.inputs.stm_image_code
        inputs['parameters'] = self.inputs.stm_image_params
        inputs['parent_calc_folder'] = self.ctx.eval_morbs.out.remote_folder
        inputs['_options'] = {
            "resources": {"num_machines": 1},
            "max_wallclock_seconds": 1600,
        } 
        
        # Need to make an explicit instance for the node to be stored to aiida
        settings = ParameterData(dict={'additional_retrieve_list': ['stm_output/*.npz']})
        inputs['settings'] = settings
        
        self.report("Inputs: " + str(inputs))
        
        future = submit(StmimageCalculation.process(), **inputs)
        return ToContext(stm_image=future)
    
    
     # ==========================================================================
    @classmethod
    def build_cp2k_inputs(cls, structure, cell, code,
                          mgrid_cutoff, wfn_file_path, elpa_switch):

        inputs = {}
        inputs['_label'] = "scf_diag"
        inputs['code'] = code
        inputs['file'] = {}

        
        atoms = structure.get_ase()  # slow

        # structure
        tmpdir = tempfile.mkdtemp()
        geom_fn = tmpdir + '/geom.xyz'
        atoms.write(geom_fn)
        geom_f = SinglefileData(file=geom_fn)
        shutil.rmtree(tmpdir)

        inputs['file']['geom_coords'] = geom_f
        
        cell_array = cell.get_array('cell')

        # parameters
        cell_abc = "%f  %f  %f" % (cell_array[0],
                                   cell_array[1],
                                   cell_array[2])
        num_machines = 12
        if len(atoms) > 500:
            num_machines = 27
        walltime = 72000
        
        wfn_file = ""
        if wfn_file_path != "":
            wfn_file = os.path.basename(wfn_file_path.value)

        inp = cls.get_cp2k_input(cell_abc,
                                 mgrid_cutoff,
                                 walltime*0.97,
                                 wfn_file,
                                 elpa_switch)

        inputs['parameters'] = ParameterData(dict=inp)

        # settings
        #settings = ParameterData(dict={'additional_retrieve_list': ['aiida-RESTART.wfn', 'BASIS_MOLOPT', 'aiida.inp']})
        #inputs['settings'] = settings

        # resources
        inputs['_options'] = {
            "resources": {"num_machines": num_machines},
            "max_wallclock_seconds": walltime,
            "append_text": ur"cp $CP2K_DATA_DIR/BASIS_MOLOPT .",
        }
        if wfn_file_path != "":
            inputs['_options']["prepend_text"] = ur"cp %s ." % wfn_file_path
        
        return inputs

    # ==========================================================================
    @classmethod
    def get_cp2k_input(cls, cell_abc, mgrid_cutoff, walltime, wfn_file, elpa_switch):

        inp = {
            'GLOBAL': {
                'RUN_TYPE': 'ENERGY',
                'WALLTIME': '%d' % walltime,
                'PRINT_LEVEL': 'LOW',
                'EXTENDED_FFT_LENGTHS': ''
            },
            'FORCE_EVAL': cls.get_force_eval_qs_dft(cell_abc,
                                                    mgrid_cutoff, wfn_file),
        }
        
        if elpa_switch:
            inp['GLOBAL']['PREFERRED_DIAG_LIBRARY'] = 'ELPA'
            inp['GLOBAL']['ELPA_KERNEL'] = 'AUTO'
            inp['GLOBAL']['DBCSR'] = {'USE_MPI_ALLOCATOR': '.FALSE.'}

        return inp

    # ==========================================================================
    @classmethod
    def get_force_eval_qs_dft(cls, cell_abc, mgrid_cutoff, wfn_file):
        force_eval = {
            'METHOD': 'Quickstep',
            'DFT': {
                'BASIS_SET_FILE_NAME': 'BASIS_MOLOPT',
                'POTENTIAL_FILE_NAME': 'POTENTIAL',
                'QS': {
                    'METHOD': 'GPW',
                    'EXTRAPOLATION': 'ASPC',
                    'EXTRAPOLATION_ORDER': '3',
                    'EPS_DEFAULT': '1.0E-14',
                },
                'MGRID': {
                    'CUTOFF': '%d' % (mgrid_cutoff),
                    'NGRIDS': '5',
                },
                'SCF': {
                    'MAX_SCF': '1000',
                    'SCF_GUESS': 'ATOMIC',
                    'EPS_SCF': '1.0E-7',
                    'ADDED_MOS': '800',
                    'CHOLESKY': 'INVERSE',
                    'DIAGONALIZATION': {
                        '_': '',
                    },
                    'SMEAR': {
                        'METHOD': 'FERMI_DIRAC',
                        'ELECTRONIC_TEMPERATURE': '300',
                    },
                    'MIXING': {
                        'METHOD': 'BROYDEN_MIXING',
                        'ALPHA': '0.1',
                        'BETA': '1.5',
                        'NBROYDEN': '8',
                    },
                    'OUTER_SCF': {
                        'MAX_SCF': '15',
                        'EPS_SCF': '1.0E-7',
                    },
                    'PRINT': {
                        'RESTART': {
                            'EACH': {
                                'QS_SCF': '0',
                                'GEO_OPT': '1',
                            },
                            'ADD_LAST': 'NUMERIC',
                            'FILENAME': 'RESTART'
                        },
                        'RESTART_HISTORY': {'_': 'OFF'}
                    }
                },
                'XC': {
                    'XC_FUNCTIONAL': {'_': 'PBE'},
                },
                'PRINT': {
                    'V_HARTREE_CUBE': {
                        'FILENAME': 'HART',
                        'STRIDE': '4 4 4',
                    },
                },
            },
            'SUBSYS': {
                'CELL': {'ABC': cell_abc},
                'TOPOLOGY': {
                    'COORD_FILE_NAME': 'geom.xyz',
                    'COORDINATE': 'xyz',
                    'CENTER_COORDINATES': {'_': ''},
                },
                'KIND': [],
            }
        }
        
        if wfn_file != "":
            force_eval['DFT']['RESTART_FILE_NAME'] = "./%s"%wfn_file
            force_eval['DFT']['SCF']['SCF_GUESS'] = 'RESTART'

        force_eval['SUBSYS']['KIND'].append({
            '_': 'Au',
            'BASIS_SET': 'DZVP-MOLOPT-SR-GTH',
            'POTENTIAL': 'GTH-PBE-q11'
        })
        force_eval['SUBSYS']['KIND'].append({
            '_': 'Ag',
            'BASIS_SET': 'DZVP-MOLOPT-SR-GTH',
            'POTENTIAL': 'GTH-PBE-q11'
        })
        force_eval['SUBSYS']['KIND'].append({
            '_': 'Cu',
            'BASIS_SET': 'DZVP-MOLOPT-SR-GTH',
            'POTENTIAL': 'GTH-PBE-q11'
        })
        force_eval['SUBSYS']['KIND'].append({
            '_': 'C',
            'BASIS_SET': 'TZV2P-MOLOPT-GTH',
            'POTENTIAL': 'GTH-PBE-q4'
        })
        force_eval['SUBSYS']['KIND'].append({
            '_': 'Br',
            'BASIS_SET': 'DZVP-MOLOPT-SR-GTH',
            'POTENTIAL': 'GTH-PBE-q7'
        })
        force_eval['SUBSYS']['KIND'].append({
            '_': 'B',
            'BASIS_SET': 'DZVP-MOLOPT-SR-GTH',
            'POTENTIAL': 'GTH-PBE-q3'
        })        
        force_eval['SUBSYS']['KIND'].append({
            '_': 'O',
            'BASIS_SET': 'TZV2P-MOLOPT-GTH',
            'POTENTIAL': 'GTH-PBE-q6'
        })
        force_eval['SUBSYS']['KIND'].append({
            '_': 'S',
            'BASIS_SET': 'TZV2P-MOLOPT-GTH',
            'POTENTIAL': 'GTH-PBE-q6'
        })
        force_eval['SUBSYS']['KIND'].append({
            '_': 'N',
            'BASIS_SET': 'TZV2P-MOLOPT-GTH',
            'POTENTIAL': 'GTH-PBE-q5'
        })
        force_eval['SUBSYS']['KIND'].append({
            '_': 'H',
            'BASIS_SET': 'TZV2P-MOLOPT-GTH',
            'POTENTIAL': 'GTH-PBE-q1'
        })

        return force_eval