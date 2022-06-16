#  Copyright 2022 Diagnostic Image Analysis Group, Radboudumc, Nijmegen, The Netherlands
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.

import json
import os
import subprocess
from pathlib import Path

import numpy as np
import SimpleITK as sitk
from evalutils import SegmentationAlgorithm
from evalutils.validators import (UniqueImagesValidator,
                                  UniquePathIndicesValidator)
from picai_prep.data_utils import atomic_image_write
from picai_prep.preprocessing import Sample


class MissingSequenceError(Exception):
    """Exception raised when a sequence is missing."""

    def __init__(self, name, folder):
        message = f"Could not find scan for {name} in {folder} (files: {os.listdir(folder)})"
        super.__init__(message)


class MultipleScansSameSequencesError(Exception):
    """Exception raised when multiple scans of the same sequences are provided."""

    def __init__(self, name, folder):
        message = f"Found multiple scans for {name} in {folder} (files: {os.listdir(folder)})"
        super.__init__(message)


class csPCaAlgorithm(SegmentationAlgorithm):
    """
    Wrapper to deploy trained baseline nnDetection model from
    https://github.com/DIAGNijmegen/picai_baseline as a
    grand-challenge.org algorithm.
    """

    def __init__(self):
        super().__init__(
            validators=dict(
                input_image=(
                    UniqueImagesValidator(),
                    UniquePathIndicesValidator(),
                )
            ),
        )

        # input / output paths for algorithm
        self.image_input_dirs = [
            "/input/images/transverse-t2-prostate-mri",
            "/input/images/transverse-adc-prostate-mri",
            "/input/images/transverse-hbv-prostate-mri",
        ]
        self.scan_paths = []
        self.cspca_detection_map_path = Path("/output/images/cspca-detection-map/cspca_detection_map.mha")
        self.case_confidence_path = Path("/output/cspca-case-level-likelihood.json")

        # input / output paths for nnDetection
        self.nndet_inp_dir = Path("/opt/algorithm/nndet/input")
        self.nndet_out_dir = Path("/opt/algorithm/nndet/output")
        self.nndet_results = Path("/opt/algorithm/results")

        # ensure required folders exist
        self.nndet_inp_dir.mkdir(exist_ok=True, parents=True)
        self.nndet_out_dir.mkdir(exist_ok=True, parents=True)
        self.cspca_detection_map_path.parent.mkdir(exist_ok=True, parents=True)

        # input validation for multiple inputs
        scan_glob_format = "*.mha"
        for folder in self.image_input_dirs:
            file_paths = list(Path(folder).glob(scan_glob_format))
            if len(file_paths) == 0:
                raise MissingSequenceError(name=folder.split("/")[-1], folder=folder)
            elif len(file_paths) >= 2:
                raise MultipleScansSameSequencesError(name=folder.split("/")[-1], folder=folder)
            else:
                # append scan path to algorithm input paths
                self.scan_paths += [file_paths[0]]

    def preprocess_input(self):
        """Preprocess input images to nnUNet Raw Data Archive format"""
        # set up Sample
        sample = Sample(
            scans=[
                sitk.ReadImage(str(path))
                for path in self.scan_paths
            ],
        )

        # perform preprocessing
        sample.preprocess()

        # write preprocessed scans to nnDetection input directory
        for i, scan in enumerate(sample.scans):
            path = self.nndet_inp_dir / f"scan_{i:04d}.nii.gz"
            atomic_image_write(scan, path)

    # Note: need to overwrite process because of flexible inputs, which requires custom data loading
    def process(self):
        """
        Load bpMRI scans and generate detection map for clinically significant prostate cancer
        """
        # perform preprocessing
        self.preprocess_input()

        # perform inference using nnDetection
        self.predict(
            task="Task2201_picai_baseline",
        )

        # convert boxes to detection map
        cmd = [
            "python",
            "/opt/code/nndet_generate_detection_maps.py",
            "--input", str(self.nndet_out_dir),
            "--output", str(self.nndet_out_dir),
        ]

        subprocess.check_call(cmd)

        # save prediction to output folder
        detection_map = sitk.ReadImage(str(self.nndet_out_dir / "scan_detection_map.nii.gz"))
        atomic_image_write(detection_map, self.cspca_detection_map_path)

        # save case-level likelihood
        with open(self.case_confidence_path, 'w') as fp:
            json.dump(float(np.max(sitk.GetArrayFromImage(detection_map))), fp)

    def predict(self, task, model="RetinaUNetV001_D3V001_3d", fold="-1"):
        """
        Use trained nnDetection network to generate boxes
        """

        # Set environment variables
        os.environ['RESULTS_FOLDER'] = str(self.nndet_results)

        # Run prediction script
        cmd = [
            'nndet', 'predict', task,
            model,
            '/workdir',
            '--fold', fold,
            '--check',
            '--input', str(self.nndet_inp_dir),
            '--output', str(self.nndet_out_dir),
            '--results', '/workdir/algorithm/results/nnDet'
        ]

        subprocess.check_call(cmd)


if __name__ == "__main__":
    csPCaAlgorithm().process()