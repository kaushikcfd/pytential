from __future__ import division, absolute_import, print_function

__copyright__ = """
Copyright (C) 2013 Andreas Kloeckner
Copyright (C) 2016 Matt Wala
"""

__license__ = """
Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
"""


import numpy as np
import numpy.linalg as la
import pyopencl as cl
import pyopencl.clmath  # noqa
import pytest
from pytools import RecordWithoutPickling
from pyopencl.tools import pytest_generate_tests_for_pyopencl \
        as pytest_generate_tests
from pytential.qbx import QBXLayerPotentialSource

from functools import partial
from meshmode.mesh.generation import (  # noqa
        ellipse, cloverleaf, starfish, drop, n_gon, qbx_peanut,
        make_curve_mesh)
from extra_curve_data import horseshoe


import logging
logger = logging.getLogger(__name__)

__all__ = ["pytest_generate_tests"]


RNG_SEED = 10
FAR_TARGET_DIST_FROM_SOURCE = 10


# {{{ utilities for iterating over panels

class ElementInfo(RecordWithoutPickling):
    """
    .. attribute:: element_nr
    .. attribute:: neighbors
    .. attribute:: discr_slice
    .. attribute:: mesh_slice
    .. attribute:: element_group
    .. attribute:: mesh_element_group
    """
    __slots__ = ["element_nr",
                 "neighbors",
                 "discr_slice"]


def iter_elements(discr):
    discr_nodes_idx = 0
    element_nr = 0
    adjacency = discr.mesh.nodal_adjacency

    for discr_group in discr.groups:
        start = element_nr
        for element_nr in range(start, start + discr_group.nelements):
            yield ElementInfo(
                element_nr=element_nr,
                neighbors=list(adjacency.neighbors[
                    slice(*adjacency.neighbors_starts[
                        element_nr:element_nr+2])]),
                discr_slice=slice(discr_nodes_idx,
                   discr_nodes_idx + discr_group.nunit_nodes))

            discr_nodes_idx += discr_group.nunit_nodes

# }}}


@pytest.mark.parametrize(("curve_name", "curve_f", "nelements"), [
    ("20-to-1 ellipse", partial(ellipse, 20), 100),
    ("horseshoe", horseshoe, 64),
    ])
def test_source_refinement(ctx_getter, curve_name, curve_f, nelements):
    cl_ctx = ctx_getter()
    queue = cl.CommandQueue(cl_ctx)

    # {{{ generate lpot source, run refiner

    order = 16
    helmholtz_k = 10

    mesh = make_curve_mesh(curve_f, np.linspace(0, 1, nelements+1), order)

    from meshmode.discretization import Discretization
    from meshmode.discretization.poly_element import \
            InterpolatoryQuadratureSimplexGroupFactory
    factory = InterpolatoryQuadratureSimplexGroupFactory(order)

    discr = Discretization(cl_ctx, mesh, factory)

    from pytential.qbx.refinement import (
            RefinerCodeContainer, refine_for_global_qbx)

    lpot_source = QBXLayerPotentialSource(discr, order)
    del discr

    lpot_source, conn = refine_for_global_qbx(
            lpot_source, RefinerCodeContainer(cl_ctx),
            factory, helmholtz_k)

    discr_nodes = lpot_source.density_discr.nodes().get(queue)
    int_centers = lpot_source.centers(-1)
    int_centers = np.array([axis.get(queue) for axis in int_centers])
    ext_centers = lpot_source.centers(+1)
    ext_centers = np.array([axis.get(queue) for axis in ext_centers])
    panel_sizes = lpot_source.panel_sizes("npanels").get(queue)

    # }}}

    # {{{ check if satisfying criteria

    def check_panel(panel):
        # Check wavenumber to panel size ratio.
        assert panel_sizes[panel.element_nr] * helmholtz_k <= 5

    def check_panel_pair(panel_1, panel_2):
        h_1 = panel_sizes[panel_1.element_nr]
        h_2 = panel_sizes[panel_2.element_nr]

        if panel_1.element_nr == panel_2.element_nr:
            # Same panel
            return

        panel_1_centers = int_centers[:, panel_1.discr_slice]
        panel_2_nodes = discr_nodes[:, panel_2.discr_slice]

        # =distance(centers of panel 1, panel 2)
        dist = (
            la.norm((
                    panel_1_centers[..., np.newaxis] -
                    panel_2_nodes[:, np.newaxis, ...]).T,
                axis=-1)
            .min())

        # Criterion 1:
        # A center cannot be closer to another panel than to its originating
        # panel.

        assert dist >= h_1 / 2, (dist, h_1, panel_1.element_nr, panel_2.element_nr)

    for panel_1 in iter_elements(lpot_source.density_discr):
        check_panel(panel_1)
        for panel_2 in iter_elements(lpot_source.density_discr):
            check_panel_pair(panel_1, panel_2)

    # }}}


@pytest.mark.parametrize(("curve_name", "curve_f", "nelements"), [
    ("20-to-1 ellipse", partial(ellipse, 20), 100),
    ("horseshoe", horseshoe, 64),
    ])
def test_target_association(ctx_getter, curve_name, curve_f, nelements):
    cl_ctx = ctx_getter()
    queue = cl.CommandQueue(cl_ctx)

    # {{{ generate lpot source

    order = 16

    # Make the curve mesh.
    mesh = make_curve_mesh(curve_f, np.linspace(0, 1, nelements+1), order)

    from meshmode.discretization import Discretization
    from meshmode.discretization.poly_element import \
            InterpolatoryQuadratureSimplexGroupFactory
    factory = InterpolatoryQuadratureSimplexGroupFactory(order)

    discr = Discretization(cl_ctx, mesh, factory)

    from pytential.qbx.refinement import QBXLayerPotentialSourceRefiner

    lpot_source = QBXLayerPotentialSource(discr, order)
    del discr
    refiner = QBXLayerPotentialSourceRefiner(cl_ctx)

    lpot_source, conn = refiner(lpot_source, factory)

    int_centers = lpot_source.centers(-1)
    ext_centers = lpot_source.centers(+1)

    # }}}

    # {{{ generate targets

    from pyopencl.clrandom import PhiloxGenerator
    rng = PhiloxGenerator(cl_ctx, seed=RNG_SEED)
    nsources = lpot_source.density_discr.nnodes
    noise = rng.uniform(queue, nsources, dtype=np.float, a=0.01, b=1.0)
    panel_sizes = lpot_source.panel_sizes("nsources").with_queue(queue)

    def targets_from_sources(sign, dist):
        from pytential import sym, bind
        dim = 2
        nodes = bind(lpot_source.density_discr, sym.nodes(dim))(queue)
        normals = bind(lpot_source.density_discr, sym.normal(dim))(queue)
        return (nodes + normals * sign * dist).as_vector(np.object)

    from pytential.target import PointsTarget

    int_targets = PointsTarget(targets_from_sources(-1, noise * panel_sizes / 2))
    ext_targets = PointsTarget(targets_from_sources(+1, noise * panel_sizes / 2))
    far_targets = PointsTarget(targets_from_sources(+1, FAR_TARGET_DIST_FROM_SOURCE))

    # Create target discretizations.
    target_discrs = (
        # On-surface targets, interior
        (lpot_source.density_discr, -1),
        # On-surface targets, exterior
        (lpot_source.density_discr, +1),
        # Interior close targets
        (int_targets, -2),
        # Exterior close targets
        (ext_targets, +2),
        # Far targets, should not need centers
        (far_targets, 0),
    )

    sizes = np.cumsum([discr.nnodes for discr, _ in target_discrs])

    (surf_int_slice,
     surf_ext_slice,
     vol_int_slice,
     vol_ext_slice,
     far_slice,
     ) = [slice(start, end) for start, end in zip(np.r_[0, sizes], sizes)]

    # }}}

    # {{{ run target associator and check

    from pytential.qbx.target_assoc import QBXTargetAssociator
    target_assoc = (
        QBXTargetAssociator(cl_ctx)(lpot_source, target_discrs)
        .get(queue=queue))

    panel_sizes = lpot_source.panel_sizes("nsources").get(queue)

    int_centers = np.array([axis.get(queue) for axis in int_centers])
    ext_centers = np.array([axis.get(queue) for axis in ext_centers])
    int_targets = np.array([axis.get(queue) for axis in int_targets.nodes()])
    ext_targets = np.array([axis.get(queue) for axis in ext_targets.nodes()])

    # Checks that the sources match with their own centers.
    def check_on_surface_targets(nsources, true_side, target_to_source_result,
                                 target_to_side_result):
        sources = np.arange(0, nsources)
        assert (target_to_source_result == sources).all()
        assert (target_to_side_result == true_side).all()

    # Checks that the targets match with centers on the appropriate side and
    # within the allowable distance.
    def check_close_targets(centers, targets, true_side,
                            target_to_source_result, target_to_side_result):
        assert (target_to_side_result == true_side).all()
        dists = la.norm((targets.T - centers.T[target_to_source_result]), axis=1)
        assert (dists <= panel_sizes[target_to_source_result] / 2).all()

    # Checks that far targets are not assigned a center.
    def check_far_targets(target_to_source_result):
        assert (target_to_source_result == -1).all()

    # Centers for source i are located at indices 2 * i, 2 * i + 1
    target_to_source = target_assoc.target_to_center // 2
    # Center side order = -1, 1, -1, 1, ...
    target_to_center_side = 2 * (target_assoc.target_to_center % 2) - 1

    check_on_surface_targets(
        nsources, -1,
        target_to_source[surf_int_slice],
        target_to_center_side[surf_int_slice])

    check_on_surface_targets(
        nsources, +1,
        target_to_source[surf_ext_slice],
        target_to_center_side[surf_ext_slice])

    check_close_targets(
        int_centers, int_targets, -1,
        target_to_source[vol_int_slice],
        target_to_center_side[vol_int_slice])

    check_close_targets(
        ext_centers, ext_targets, +1,
        target_to_source[vol_ext_slice],
        target_to_center_side[vol_ext_slice])

    check_far_targets(
        target_to_source[far_slice])

    # }}}


def test_target_association_failure(ctx_getter):
    cl_ctx = ctx_getter()
    queue = cl.CommandQueue(cl_ctx)

    # {{{ generate circle

    order = 5
    nelements = 40

    # Make the curve mesh.
    curve_f = partial(ellipse, 1)
    mesh = make_curve_mesh(curve_f, np.linspace(0, 1, nelements+1), order)

    from meshmode.discretization import Discretization
    from meshmode.discretization.poly_element import \
            InterpolatoryQuadratureSimplexGroupFactory
    factory = InterpolatoryQuadratureSimplexGroupFactory(order)
    discr = Discretization(cl_ctx, mesh, factory)
    lpot_source = QBXLayerPotentialSource(discr, order)

    # }}}

    # {{{ generate targets and check

    close_circle = 0.999 * np.exp(
        2j * np.pi * np.linspace(0, 1, 500, endpoint=False))
    from pytential.target import PointsTarget
    close_circle_target = (
        PointsTarget(cl.array.to_device(
            queue, np.array([close_circle.real, close_circle.imag]))))

    targets = (
        (close_circle_target, 0),
        )

    from pytential.qbx.target_assoc import (
        QBXTargetAssociator, QBXTargetAssociationFailedException)

    with pytest.raises(QBXTargetAssociationFailedException):
        QBXTargetAssociator(cl_ctx)(lpot_source, targets)

    # }}}


# You can test individual routines by typing
# $ python test_layer_pot.py 'test_routine()'

if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        exec(sys.argv[1])
    else:
        from py.test.cmdline import main
        main([__file__])

# vim: fdm=marker
