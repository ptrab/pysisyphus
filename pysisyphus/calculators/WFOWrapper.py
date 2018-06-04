#!/usr/bin/env python3

from collections import OrderedDict
import itertools
import logging
from pathlib import Path
import shutil
import subprocess
import tempfile

import numpy as np
# try:
    # from pyscf import gto
# except ImportError:
    # print("Couldn't import pyscf!")
import pyparsing as pp

from pysisyphus.config import Config
from pysisyphus.helpers import chunks


CIOVL="""mix_aoovl=ao_ovl
a_mo=mos.1
b_mo=mos.2
a_det=dets.1
b_det=dets.2
a_mo_read=2
b_mo_read=2"""

CIOVL_NO_SAO="""ao_read=-1
same_aos=.true.
a_mo=mos.1
b_mo=mos.2
a_det=dets.1
b_det=dets.2
a_mo_read=2
b_mo_read=2"""


class WFOWrapper:
    logger = logging.getLogger("wfoverlap")
    matrix_types = OrderedDict((
        ("ovlp", "Overlap matrix"),
        ("renorm", "Renormalized overlap matrix"),
        ("ortho", "Orthonormalized overlap matrix")
    ))

    def __init__(self, occ_mos, virt_mos, basis, charge, calc_number=0,
                 conf_thresh=1e-4, out_dir="./"):
        self.base_cmd = Config["wfoverlap"]["cmd"]
        self.occ_mos = occ_mos
        self.virt_mos = virt_mos
        self.basis = basis
        self.charge = charge
        # Should correspond to the attribute of the parent calculator
        self.calc_number = calc_number
        self.conf_thresh = conf_thresh
        self.out_dir = Path(out_dir).resolve()
        self.name = f"WFOWrapper_{self.calc_number}"
        self.mos = self.occ_mos + self.virt_mos
        self.base_det_str = "d"*self.occ_mos + "e"*self.virt_mos
        self.fmt = "{: .10f}"

        self.iter_counter = 0

        # Molecule coordinates
        self.coords_list = list()
        # MO coefficients
        self.mos_list = list()
        self.ci_coeffs_list = list()
        self.mo_inds_list = list()
        self.from_set_list = list()
        self.to_set_list = list()

    @property
    def last_two_coords(self):
        return self.coords_list[-2:]

    def log(self, message):
        self.logger.debug(f"{self.name}, " + message)

    def fake_turbo_mos(self, mo_coeffs):
        """Create a mos file suitable for TURBOMOLE input. All MO eigenvalues
        are set to 0.0. There is also a little deviation in the formatting
        (see turbo_fmt()) but it works ..."""

        def turbo_fmt(num):
            """Not quite the real TURBOMOLE format, but it works ...
            In TURBOMOLE the first character is always 0 for positive doubles
            and - for negative doubles."""
            return f"{num:+20.13E}".replace("E", "D")

        base = "$scfmo    scfconv=7  format(4d20.14)\n# from pysisyphus\n" \
               "{mo_strings}\n$end"

        # WFOverlap expects the string eigenvalue starting at 16, so we have
        mo_str = "{mo_index:>6d}  a      eigenvalue=-.00000000000000D+00   " \
                 "nsaos={nsaos}\n{joined}"
        nsaos = mo_coeffs.shape[0]

        mo_strings = list()
        for mo_index, mo in enumerate(mo_coeffs, 1):
            in_turbo_fmt = [turbo_fmt(c) for c in mo]
            # Combine into chunks of four
            lines = ["".join(chnk) for chnk in chunks(in_turbo_fmt, 4)]
            # Join the lines
            joined = "\n".join(lines)
            mo_strings.append(mo_str.format(mo_index=mo_index, nsaos=nsaos,
                                            joined=joined))
        return base.format(mo_strings="\n".join(mo_strings))

    def build_mole(self, coords):
        coords3d = coords.reshape(-1, 3)
        return [[atom, c] for atom, c in zip(self.atoms, coords3d)]

    def get_ao_overlap(self, a1, a2, sphere=True):
        def prepare(atom):
            mol = gto.Mole()
            mol.atom = atom
            mol.basis = self.basis
            mol.unit = "bohr"
            mol.charge = self.charge
            mol.build()
            return mol
        mol1 = prepare(a1)
        mol2 = prepare(a2)
        if sphere:
            ao_ovlp = gto.mole.intor_cross("int1e_ovlp_sph", mol1, mol2)
        else:
            # Cartesian functions otherwise
            ao_ovlp = gto.mole.intor_cross("int1e_ovlp_cart", mol1, mol2)
        return ao_ovlp

    def ci_coeffs_above_thresh(self, eigenpair, thresh=1e-5):
        arr = np.array(eigenpair)
        arr = arr.reshape(self.occ_mos, -1)
        mo_inds = np.where(np.abs(arr) > thresh)
        #ci_coeffs = arr[mo_inds]
        #vals_sq = vals**2
        #for from_mo, to_mo, vsq, v in zip(*inds, vals_sq, vals):
        #    print(f"\t{from_mo+1} -> {to_mo+1+self.occ_mos} {v:.04f} {vsq:.02f}")
        #return ci_coeffs, mo_inds
        return arr, mo_inds

    def make_det_string(self, inds):
        """Return spin adapted strings."""
        from_mo, to_mo = inds
        # Until now the first virtual MO (to_mo) has index 0. To subsitute
        # the base_str at the correct index we have to increase all to_mo
        # indices by the number off occupied MO.
        to_mo += self.occ_mos
        # Make string for excitation of an alpha electron
        ab = list(self.base_det_str)
        ab[from_mo] = "b"
        ab[to_mo] = "a"
        ab_str = "".join(ab)
        # Make string for excitation of an beta electron
        ba = list(self.base_det_str)
        ba[from_mo] = "a"
        ba[to_mo] = "b"
        ba_str = "".join(ba)
        return ab_str, ba_str

    def generate_all_dets(self, occ_set1, virt_set1, occ_set2, virt_set2):
        """Generate all possible single excitation determinant strings
        from union(occ_mos) to union(virt_mos)."""
        # Unite the respective sets of both calculations
        occ_set = occ_set1 | occ_set2
        virt_set = virt_set1 | virt_set2
        # Genrate all possible excitations (combinations) from the occupied
        # MO set to (and) the virtual MO set.
        all_inds = [(om, vm) for om, vm
                    in itertools.product(occ_set, virt_set)]
        det_strings = [self.make_det_string(inds) for inds in all_inds]
        return all_inds, det_strings

    def make_full_dets_list(self, all_inds, det_strings, ci_coeffs):
        dets_list = list()
        for inds, det_string in zip(all_inds, det_strings):
            ab, ba = det_string
            from_mo, to_mo = inds
            per_state =  ci_coeffs[:,from_mo,to_mo]
            # Drop unimportant configurations, that are configurations
            # having low weights in all states under consideration.
            if np.sum(per_state**2) < self.conf_thresh:
                continue
            # A singlet determinant can be formed in two ways:
            # (up down) (up down) (up down) ...
            # or
            # (down up) (down up) (down up) ...
            # We take this into account by expanding the singlet determinants
            # and using a proper normalization constant.
            # See 10.1063/1.3000012 Eq. (5) and 10.1021/acs.jpclett.7b01479 SI
            per_state *= 1/2**0.5
            as_str = lambda arr: " ".join([self.fmt.format(cic)
                                           for cic in arr])
            ps_str = as_str(per_state)
            mps_str = as_str(-per_state)
            dets_list.append(f"{ab}\t{ps_str}")
            dets_list.append(f"{ba}\t{mps_str}")
        return dets_list

    def set_from_nested_list(self, nested):
        return set([i for i in itertools.chain(*nested)])

    def store_iteration(self, atoms, coords, mos, eigenpair_list):
        coeffs_inds = [self.ci_coeffs_above_thresh(ep)
                       for ep in eigenpair_list]
        ci_coeffs, mo_inds = zip(*coeffs_inds)
        ci_coeffs = np.array(ci_coeffs)

        from_mos, to_mos = zip(*mo_inds)
        from_set = self.set_from_nested_list(from_mos)
        to_set = self.set_from_nested_list(to_mos)

        self.atoms = atoms
        self.mos_list.append(mos)
        self.coords_list.append(coords)
        self.ci_coeffs_list.append(ci_coeffs)
        self.mo_inds_list.append(mo_inds)
        self.from_set_list.append(from_set)
        self.to_set_list.append(to_set)
        self.iter_counter += 1
        self.log(f"Stored iteration {self.iter_counter}")

    def get_iteration(self, ind):
        return (self.mos_list[ind], self.coords_list[ind],
                self.ci_coeffs_list[ind], self.mo_inds_list[ind],
                self.from_set_list[ind], self.to_set_list[ind])

    def make_dets_header(self, cic, dets_list):
        return f"{len(cic)} {self.mos} {len(dets_list)}"

    def parse_wfoverlap_out(self, text, type_="ortho"):
        """Returns overlap matrix."""
        header_str = self.matrix_types[type_] + " <PsiA_i|PsiB_j>"
        header = pp.Literal(header_str)
        float_ = pp.Word(pp.nums+"-.")
        psi_bra = pp.Literal("<Psi") + pp.Word(pp.alphas) \
                  + pp.Word(pp.nums) + pp.Literal("|")
        psi_ket = pp.Literal("|Psi") + pp.Word(pp.alphas) \
                  + pp.Word(pp.nums) + pp.Literal(">")
        matrix_line = pp.Suppress(psi_bra) + pp.OneOrMore(float_)

        parser = pp.SkipTo(header, include=True) \
                 + pp.OneOrMore(psi_ket) \
                 + pp.OneOrMore(matrix_line).setResultsName("overlap")

        result = parser.parseString(text)
        return np.array(list(result["overlap"]), dtype=np.float)

    def wf_overlap(self, iter1, iter2, ao_ovlp=None):
        mos1, coords1, cic1, moi1, fs1, ts1 = iter1
        mos2, coords2, cic2, moi2, fs2, ts2 = iter2
        # Create a fake array for the ground state where all CI coefficients
        # are zero and add it.
        gs_cic = np.zeros_like(cic1[0])
        cic1_with_gs = np.concatenate((gs_cic[None,:,:], cic1))
        cic2_with_gs = np.concatenate((gs_cic[None,:,:], cic2))
        mol1 = self.build_mole(coords1)
        mol2 = self.build_mole(coords2)

        #ao_ovlp = self.get_ao_overlap(mol1, mol2)

        all_inds, det_strings = self.generate_all_dets(fs1, ts1, fs2, ts2)
        # Prepare line for ground state
        gs_coeffs = np.zeros(len(cic1_with_gs))
        # Ground state is 100% HF configuration
        gs_coeffs[0] = 1
        gs_coeffs_str = " ".join([self.fmt.format(c)
                                  for c in gs_coeffs])
        gs_line = f"{self.base_det_str}\t{gs_coeffs_str}"
        dets1 = [gs_line] + self.make_full_dets_list(all_inds,
                                                     det_strings,
                                                     cic1_with_gs)
        dets2 = [gs_line] + self.make_full_dets_list(all_inds,
                                                     det_strings,
                                                     cic2_with_gs)
        header1 = self.make_dets_header(cic1_with_gs, dets1)
        header2 = self.make_dets_header(cic2_with_gs, dets2)

        backup_path = self.out_dir / f"wfo_{self.calc_number}.{self.iter_counter:03d}"
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            self.log(f"Calculation in {tmp_dir}")
            #import pdb; pdb.set_trace()
            mos1_path = shutil.copy(mos1, tmp_path / "mos.1")
            mos2_path = shutil.copy(mos2, tmp_path / "mos.2")
            dets1_path = tmp_path / "dets.1"
            with open(dets1_path, "w") as handle:
                handle.write(header1+"\n"+"\n".join(dets1))
            dets2_path = tmp_path / "dets.2"
            with open(dets2_path, "w") as handle:
                handle.write(header2+"\n"+"\n".join(dets2))

            # Decide wether to use a double molecule overlap matrix or
            # (approximately) reconstruct the ao_ovlp matrix from the MO
            # coefficients.
            if ao_ovlp is None:
                ciovl_in = CIOVL_NO_SAO
                self.log("Got no ao_ovl-matrix. Using ao_read=-1 and "
                         "same_aos=.true. to reconstruct the AO-overlap matrix!")
            else:
                ciovl_in = CIOVL
                ao_header = "{} {}".format(*ao_ovlp.shape)
                ao_ovl_path = tmp_path / "ao_ovl"
                np.savetxt(ao_ovl_path, ao_ovlp, fmt="%22.15E", header=ao_header,
                           comments="")

            ciovl_fn = "ciovl.in"
            with open(tmp_path / ciovl_fn, "w") as handle:
                handle.write(ciovl_in)

            # Create a backup of the whole temporary directory
            try:
                shutil.rmtree(backup_path)
            except FileNotFoundError:
                pass
            shutil.copytree(tmp_dir, backup_path)

            cmd = f"{self.base_cmd} -m 4000 -f {ciovl_fn}".split()
            result = subprocess.Popen(cmd, cwd=tmp_path,
                                      stdout=subprocess.PIPE)
            result.wait()
            stdout = result.stdout.read().decode("utf-8")
        if "differs significantly" in stdout:
            self.log("WARNING: Orthogonalized matrix differs significantly "
                     "from original matrix! There is probably mixing with "
		     "external states.")

        wfo_log_fn = self.out_dir / f"wfo_{self.calc_number}.{self.iter_counter:03d}.out"
        with open(wfo_log_fn, "w") as handle:
            handle.write(stdout)
        # Also copy the WFO-output to the input backup
        shutil.copy(wfo_log_fn, backup_path)

        matrices = [self.parse_wfoverlap_out(stdout, type_=key)
                    for key in self.matrix_types.keys()]

        reshaped_mats = [mat.reshape(-1, len(cic2_with_gs))
                         for mat in matrices]
        for key, mat in zip(self.matrix_types.keys(), reshaped_mats):
            mat_fn = backup_path / f"{key}_mat.dat"
            np.savetxt(mat_fn, mat)

        # for mat in reshaped_mats:
            # print(mat)
        return reshaped_mats

    def track(self, old_root, ao_ovlp=None):
        """Return the root with the highest overlap in the latest iteration
        compared to old_root in the previous to last iteration."""

        if not isinstance(old_root, int):
            raise Exception("Please provide an integer value for old_root!")
        self.log(f"Previous root is {old_root}.")

        iter1 = self.get_iteration(-2)
        iter2 = self.get_iteration(-1)
        overlap_mats = self.wf_overlap(iter1, iter2, ao_ovlp=ao_ovlp)
        overlap_matrix = overlap_mats[1]
        old_root_col = overlap_matrix[old_root]**2
        new_root = old_root_col.argmax()
        max_overlap = old_root_col[new_root]
        old_root_col_str = ", ".join(
            [f"{i}: {ov:.2%}" for i, ov in enumerate(old_root_col)]
        )
        self.log(f"Overlaps: {old_root_col_str}")
        if new_root == old_root:
            msg = f"New root is {new_root}, keeping previous root. Overlap is " \
                  f"{max_overlap:.2%}."
        else:
            msg = f"Root flip! New root {new_root} has {max_overlap:.2%} " \
                  f"overlap with previous root {old_root}."
        self.log(msg)
        return int(new_root)

    def compare(self, wfow_B, ao_ovlp=None):
        """Calculate wavefunction overlaps between the two WFOWrapper objects,
        using the last stored iterations respectively."""
        iter1 = self.get_iteration(-1)
        iter2 = wfow_B.get_iteration(-1)
        overlap_mats = self.wf_overlap(iter1, iter2, ao_ovlp)
        overlap_matrix = overlap_mats[1]
        """argmax(axis=1) returns the index of the highest overlap
        along every column, that is for every state in wfow_B.
        The n-th item in max_ovlp_inds gives the index of the highest
        overlap in wfow_A with the n-th state in wfow_B.

        Using argmax(axis=0) would yield the highest overlap along
        every row. The n-th item in max_ovlp_inds would give the index
        of the highest overlapping state of wfow_B with the n-th state
        in wfow_A.
        """
        max_ovlp_inds = (overlap_matrix**2).argmax(axis=1)
        if np.unique(max_ovlp_inds).size != max_ovlp_inds.size:
            self.log("Assignment of states is ambiguous! Check the results "
                     "carefully!")
        ovlps = (overlap_matrix**2).max(axis=1)
        inds1 = np.arange(max_ovlp_inds.size)
        #for i1, i2, o in zip(inds1, max_ovlp_inds, ovlps):
        #    print(f"{i1} -> {i2} ({o:.1%} overlap)")
        return max_ovlp_inds

    def __str__(self):
        return self.name
