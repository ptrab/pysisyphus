from collections import namedtuple
import itertools as it

import numpy as np
from scipy.spatial.distance import pdist, squareform

from pysisyphus.constants import BOHR2ANG
from pysisyphus.helpers_pure import log, sort_by_central, merge_sets
from pysisyphus.elem_data import VDW_RADII, COVALENT_RADII as CR
from pysisyphus.intcoords import Stretch, Bend, LinearBend, Torsion
from pysisyphus.intcoords.valid import bend_valid, dihedral_valid


def get_pair_covalent_radii(atoms):
    atoms = [a.lower() for a in atoms]
    cov_radii = np.array([CR[a] for a in atoms])
    pair_cov_radii = np.array([r1 + r2 for r1, r2 in it.combinations(cov_radii, 2)])
    return pair_cov_radii


def get_bond_mat(geom, bond_factor=1.3):
    cdm = pdist(geom.coords3d)
    pair_cov_radii = get_pair_covalent_radii(geom.atoms)
    bond_mat = squareform(cdm <= (pair_cov_radii * bond_factor))
    return bond_mat


def get_bond_sets(atoms, coords3d, bond_factor=1.3, return_cdm=False, return_cbm=False):
    cdm = pdist(coords3d)
    # Generate indices corresponding to the atom pairs in the
    # condensed distance matrix cdm.
    atom_inds = list(it.combinations(range(len(coords3d)), 2))
    atom_inds = np.array(atom_inds, dtype=int)
    scaled_cr_sums = bond_factor * get_pair_covalent_radii(atoms)
    # condensed bond matrix
    cbm = cdm <= scaled_cr_sums
    bond_inds = atom_inds[cbm]
    if not return_cbm and not return_cdm:
        return bond_inds
    add_returns = tuple(
        [mat for flag, mat in ((return_cdm, cdm), (return_cbm, cbm)) if flag]
    )
    return (bond_inds,) + add_returns


def get_fragments(atoms, coords):
    coords3d = coords.reshape(-1, 3)
    # Bond indices without interfragment bonds and/or hydrogen bonds
    bond_inds = get_bond_sets(atoms, coords3d)

    bond_ind_sets = [frozenset(bi) for bi in bond_inds]
    fragments = merge_sets(bond_ind_sets)

    return fragments


def connect_fragments(cdm, fragments, max_aux=3.78, aux_factor=1.3, logger=None):
    """Determine the smallest interfragment bond for a list
    of fragments and a condensed distance matrix."""
    if len(fragments) > 1:
        log(
            logger,
            f"Detected {len(fragments)} fragments. Generating interfragment bonds.",
        )
    dist_mat = squareform(cdm)
    interfrag_inds = list()
    aux_interfrag_inds = list()
    for frag1, frag2 in it.combinations(fragments, 2):
        log(logger, f"\tConnecting {len(frag1)} atom and {len(frag2)} atom fragment")
        inds = [(i1, i2) for i1, i2 in it.product(frag1, frag2)]
        distances = np.array([dist_mat[ind] for ind in inds])

        # Determine minimum distance bond
        min_ind = distances.argmin()
        min_dist = distances[min_ind]
        interfrag_bond = tuple(inds[min_ind])
        interfrag_inds.append(interfrag_bond)
        log(logger, f"\tMinimum distance bond: {interfrag_bond}, {min_dist:.4f} au")

        # Determine auxiliary interfragment bonds that are either below max_aux
        # (default 2 Å, ≈ 3.78 au), or less than aux_factor (default 1.3) times the
        # minimum interfragment distance.
        below_max_aux = [
            ind for ind in inds if (dist_mat[ind] < max_aux) and (ind != interfrag_bond)
        ]
        if below_max_aux:
            ang_max_aux = max_aux * BOHR2ANG
            log(
                logger,
                f"\tAux. interfrag bonds below {ang_max_aux:.2f} Å:\n"
                + "\n".join(
                    [f"\t\t{ind}: {dist_mat[ind]:.4f} au" for ind in below_max_aux]
                ),
            )
        scaled_min_dist = aux_factor * min_dist
        above_min_dist = [
            ind
            for ind in inds
            if (dist_mat[ind] < scaled_min_dist)
            and (ind != interfrag_bond)
            and (ind not in below_max_aux)
        ]
        if above_min_dist:
            ang_max_aux = max_aux * BOHR2ANG
            log(
                logger,
                f"\tAux. interfrag bonds below {aux_factor:.2f} * min_dist:\n"
                + "\n".join(
                    [f"\t\t{ind}: {dist_mat[ind]:.4f} au" for ind in above_min_dist]
                ),
            )
        aux_interfrag_inds.extend(below_max_aux)
        aux_interfrag_inds.extend(above_min_dist)
    # Or as Philipp proposed: two loops over the fragments and only
    # generate interfragment distances. So we get a full matrix with
    # the original indices but only the required distances.
    return interfrag_inds, aux_interfrag_inds


def get_hydrogen_bond_inds(atoms, coords3d, bond_inds, logger=None):
    tmp_sets = [frozenset(bi) for bi in bond_inds]
    # Check for hydrogen bonds as described in [1] A.1 .
    # Find hydrogens bonded to small electronegative atoms X = (N, O
    # F, P, S, Cl).
    hydrogen_inds = [i for i, a in enumerate(atoms) if a.lower() == "h"]
    x_inds = [i for i, a in enumerate(atoms) if a.lower() in "n o f p s cl".split()]
    hydrogen_bond_inds = list()
    for h_ind, x_ind in it.product(hydrogen_inds, x_inds):
        as_set = set((h_ind, x_ind))
        if as_set not in tmp_sets:
            continue
        # Check if distance of H to another electronegative atom Y is
        # greater than the sum of their covalent radii but smaller than
        # the 0.9 times the sum of their van der Waals radii. If the
        # angle X-H-Y is greater than 90° a hydrogen bond is asigned.
        y_inds = set(x_inds) - set((x_ind,))
        for y_ind in y_inds:
            y_atom = atoms[y_ind].lower()
            cov_rad_sum = CR["h"] + CR[y_atom]
            distance = Stretch._calculate(coords3d, (h_ind, y_ind))
            vdw = 0.9 * (VDW_RADII["h"] + VDW_RADII[y_atom])
            angle = Bend._calculate(coords3d, (x_ind, h_ind, y_ind))
            if (cov_rad_sum < distance < vdw) and (angle > np.pi / 2):
                hydrogen_bond_inds.append((h_ind, y_ind))
                log(
                    logger,
                    f"Detected hydrogen bond between atoms {h_ind} "
                    f"({atoms[h_ind]}) and {y_ind} ({atoms[y_ind]})",
                )

    return hydrogen_bond_inds


def get_bend_inds(coords3d, bond_inds, min_deg, max_deg, logger=None):
    bond_sets = {frozenset(bi) for bi in bond_inds}

    bend_inds = list()
    for bond_set1, bond_set2 in it.combinations(bond_sets, 2):
        union = bond_set1 | bond_set2
        if len(union) == 3:
            indices, _ = sort_by_central(bond_set1, bond_set2)
            if not bend_valid(coords3d, indices, min_deg, max_deg):
                log(logger, f"Bend {indices} is not valid!")
                continue
            bend_inds.append(indices)

    return bend_inds


def get_linear_bend_inds(coords3d, cbm, bend_inds, min_deg, max_bonds, logger=None):
    bm = squareform(cbm)
    linear_bend_inds = list()
    for bend in bend_inds:
        deg = np.rad2deg(Bend._calculate(coords3d, bend))
        bonds = sum(bm[bend[1]])
        if (deg >= min_deg) and (bonds <= max_bonds):
            log(
                logger,
                f"Bend {bend}={deg:.1f}° is (close to) linear. "
                "Creating linear bend & complement.",
            )
            linear_bend_inds.append(bend)
    return linear_bend_inds


def get_dihedral_inds(coords3d, bond_inds, bend_inds, max_deg=175.0, logger=None):
    max_rad = np.deg2rad(max_deg)
    max_rad_thresh = np.pi - max_rad
    bond_dict = dict()
    for from_, to_ in bond_inds:
        bond_dict.setdefault(from_, list()).append(to_)
        bond_dict.setdefault(to_, list()).append(from_)
    dihedrals = list()
    dihedral_inds = list()
    improper_dihedrals = list()

    def log_dihed_skip(inds):
        log(
            logger,
            f"Skipping generation of dihedral {inds} "
            "as some of the the atoms are (close too) linear.",
        )

    def set_dihedral_index(dihedral_ind):
        dihed = tuple(dihedral_ind)
        # Check if this dihedral is already present
        if (dihed in dihedrals) or (dihed[::-1] in dihedrals):
            return
        # Assure that the angles are below 175° (3.054326 rad)
        if not dihedral_valid(coords3d, dihedral_ind, deg_thresh=max_deg):
            log_dihed_skip(dihedral_ind)
            return
        dihedral_inds.append(dihedral_ind)
        dihedrals.append(dihed)

    for bond, bend in it.product(bond_inds, bend_inds):
        central = bend[1]
        bend_set = set(bend)
        bond_set = set(bond)
        # Check if the two sets share one common atom. If not continue.
        intersect = bend_set & bond_set
        if len(intersect) != 1:
            continue

        # When the common atom between bond and bend is a terminal, and not a central atom
        # in the bend we create a proper dihedral. Improper dihedrals are only created
        # when no proper dihedrals have been found.
        if central not in bond_set:
            # The new terminal atom in the dihedral is the one, that doesn' intersect.
            terminal = tuple(bond_set - intersect)[0]
            intersecting_atom = tuple(intersect)[0]
            bend_terminal = tuple(bend_set - {central} - intersect)[0]

            bend_rad = Bend._calculate(coords3d, bend)
            # Bend atoms are nearly collinear. Check if we can skip the central bend atom.
            if bend_rad >= max_rad:
                bend_terminal_bonds = set(bond_dict[bend_terminal]) - {central}
                set_dihedrals = [
                    (terminal, intersecting_atom, bend_terminal, btb)
                    for btb in bend_terminal_bonds
                ]
            elif intersecting_atom == bend[0]:
                set_dihedrals = [[terminal] + list(bend)]
            else:
                set_dihedrals = [list(bend) + [terminal]]
            [set_dihedral_index(dihed) for dihed in set_dihedrals]
        # If the common atom is the central atom we try to form an out
        # of plane bend / improper torsion. They may be created later on.
        else:
            fourth_atom = list(bond_set - intersect)
            dihedral_ind = list(bend) + fourth_atom
            # This way dihedrals may be generated that contain linear
            # atoms and these would be undefinied. So we check for this.
            if dihedral_valid(coords3d, dihedral_ind, deg_thresh=max_deg):
                improper_dihedrals.append(dihedral_ind)
            else:
                log_dihed_skip(dihedral_ind)

    # Now try to create the remaining improper dihedrals.
    if (len(coords3d) >= 4) and (len(dihedral_inds) == 0):
        for improp in improper_dihedrals:
            set_dihedral_index(improp)
        log(
            logger,
            "Permutational symmetry not considerd in generation of "
            "improper dihedrals.",
        )

    return dihedral_inds


def sort_by_prim_type(to_sort=None):
    if to_sort is None:
        to_sort = list()

    by_prim_type = [[], [], []]
    for item in to_sort:
        len_ = len(item)
        # len -> index
        #   2 ->     0 (bond)
        #   3 ->     1 (bend)
        #   4 ->     2 (torsion)
        by_prim_type[len_ - 2].append(item)
    return by_prim_type


CoordInfo = namedtuple(
    "CoordInfo",
    "bonds hydrogen_bonds interfrag_bonds aux_interfrag_bonds bends linear_bends "
    "dihedrals fragments cdm cbm".split(),
)


def setup_redundant(
    atoms,
    coords3d,
    factor=1.3,
    define_prims=None,
    min_deg=15,
    max_deg=180,
    complement_deg=175,
    lb_min_deg=None,
    lb_max_bonds=4,
    min_weight=None,
    logger=None,
):
    log(logger, f"Detecting primitive internals for {len(atoms)} atoms.")
    # Additional primitives to be defined.
    def_bonds, def_bends, def_dihedrals = sort_by_prim_type(define_prims)

    def keep_coord(prim_cls, prim_inds):
        return (
            True
            if (min_weight is None)
            else (prim_cls._weight(atoms, coords3d, prim_inds, 0.12) >= min_weight)
        )

    # Bonds
    bond_inds, cdm, cbm = get_bond_sets(
        atoms,
        coords3d,
        bond_factor=factor,
        return_cdm=True,
        return_cbm=True,
    )
    bond_inds = [tuple(bond) for bond in bond_inds]
    bond_inds += def_bonds
    bond_ind_sets = [frozenset(bond) for bond in bond_inds if keep_coord(Stretch, bond)]

    # Fragments
    fragments = merge_sets(bond_ind_sets)
    # Check for unbonded single atoms and create fragments for them.
    bonded_set = set(tuple(np.ravel(bond_inds)))
    unbonded_set = set(range(len(atoms))) - bonded_set
    fragments.extend([frozenset((atom,)) for atom in unbonded_set])

    # Check for disconnected fragments. If they are present, create interfragment
    # bonds between them.
    interfrag_inds, aux_interfrag_inds = connect_fragments(
        cdm, fragments, logger=logger
    )

    # Hydrogen bonds
    hydrogen_bond_inds = get_hydrogen_bond_inds(
        atoms, coords3d, bond_inds, logger=logger
    )
    # Don't use auxilary interfragment bonds for bend detection
    bonds_for_bends = set(
        [frozenset(bond) for bond in bond_inds + hydrogen_bond_inds + interfrag_inds]
    )

    # Bends
    bend_inds = get_bend_inds(
        coords3d,
        bonds_for_bends,
        min_deg=min_deg,
        max_deg=max_deg,
        logger=logger,
    )
    # All bends will be checked, for being linear bends and will be removed from
    # bend_inds, if needed.
    bend_inds += def_bends
    bend_inds = [bend for bend in bend_inds if keep_coord(Bend, bend)]

    # Linear Bends
    linear_bend_inds = list()
    if lb_min_deg is not None:
        linear_bend_inds = get_linear_bend_inds(
            coords3d,
            cbm,
            bend_inds,
            min_deg=lb_min_deg,
            max_bonds=lb_max_bonds,
            logger=logger,
        )
        bend_inds = [bend for bend in bend_inds if bend not in linear_bend_inds]

    # Dihedrals
    dihedral_inds = get_dihedral_inds(
        coords3d, bonds_for_bends, bend_inds, logger=logger
    )
    dihedral_inds += def_dihedrals
    dihedral_inds = [
        dihedral for dihedral in dihedral_inds if keep_coord(Torsion, dihedral)
    ]

    coord_info = CoordInfo(
        bonds=bond_inds,
        hydrogen_bonds=hydrogen_bond_inds,
        interfrag_bonds=interfrag_inds,
        aux_interfrag_bonds=aux_interfrag_inds,
        bends=bend_inds,
        linear_bends=linear_bend_inds,
        dihedrals=dihedral_inds,
        fragments=fragments,
        cdm=cdm,
        cbm=cbm,
    )
    return coord_info


def setup_redundant_from_geom(geom, *args, **kwargs):
    return setup_redundant(geom.atoms, geom.coords3d, *args, **kwargs)


def get_primitives(
    coords3d,
    bond_inds,
    bend_inds,
    linear_bend_inds,
    dihedral_inds,
    make_complement,
    logger=None,
):
    zipped = (
        (bond_inds, Stretch),
        (bend_inds, Bend),
        (linear_bend_inds, LinearBend),
        (dihedral_inds, Torsion),
    )

    primitives = list()
    for per_type, prim_cls in zipped:
        for prim_inds in per_type:
            prim_kwargs = {
                "indices": prim_inds,
                "periodic": len(prim_inds) == 4,
            }

            # Create primitive coordinate and append
            prim = prim_cls(**prim_kwargs)
            primitives.append(prim)

            if isinstance(prim, LinearBend) and make_complement:
                log(logger, f"Created complement for LinearBend {prim_inds}")
                prim_kwargs["complement"] = True
                prim = prim_cls(**prim_kwargs)
                primitives.append(prim)

    msg = (
        "Defined primitives\n"
        + "\n".join(
            [f"\t{i:03d}: {str(p.indices): >14}" for i, p in enumerate(primitives)]
        )
        + "\n"
    )
    log(logger, msg)
    return primitives
