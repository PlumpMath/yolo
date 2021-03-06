#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
BKZ 2.0 variant which ensures that the basis quality does not decrease.
"""

from fpylll.algorithms.bkz_stats import BKZTreeTracer, dummy_tracer
import begin
import time
import copy
from fpylll.algorithms.bkz2 import BKZReduction as BKZ2
from fpylll import BKZ, IntegerMatrix, Enumeration, EnumerationError, LLL, GSO
from fpylll.util import adjust_radius_to_gh_bound, set_random_seed


class BKZReduction(BKZ2):
    def copy_block(self, kappa, block_size):
        """
        Append a copy of the block from ``kappa`` to ``kappa + block_size`` to the end of the
        matrix.

        :param kappa: start index
        :param block_size: number of vectors to append

        """
        for i in range(block_size):
            self.M.create_row()

        with self.M.row_ops(self.M.d-block_size, self.M.d):
            for i in range(block_size):
                self.M.row_addmul(self.M.d-block_size+i, kappa+i, 1)

    def delete_copy_block(self, kappa, block_size, restore):
        """Delete copy of block from ``kappa`` to ``kappa + block_size``

        If ``restore`` is ``True`` then copy it back to the block starting at``kappa``

        :param kappa: start index.
        :param block_size: number of vectors to consider
        :param restore: copy block back

        """
        if restore:
            for i in range(block_size):
                # this implements a swap
                self.M.move_row(self.M.d-block_size+i, kappa+i)
                self.M.move_row(kappa+i+1, self.M.d-block_size+i)

        for i in range(block_size):
            self.M.remove_last_row()

    def __call__(self, params, min_row=0, max_row=-1):
        """Run the BKZ algorithm with parameters `param`.

        :param params: BKZ parameters
        :param min_row: start processing in this row
        :param max_row: stop processing in this row (exclusive)

        """
        tracer = BKZTreeTracer(self, verbosity=params.flags & BKZ.VERBOSE, start_clocks=True)

        auto_abort = BKZ.AutoAbort(self.M, self.A.nrows)
        cputime_start = time.clock()

        with tracer.context("lll"):
            self.lll_obj()

        i = 0
        while True:
            with tracer.context("tour", i):
                self.tour(params, min_row, max_row, tracer, top_level=True)
            i += 1
            if params.block_size >= self.A.nrows:
                break
            if auto_abort.test_abort():
                break
            if (params.flags & BKZ.MAX_LOOPS) and i >= params.max_loops:
                break
            if (params.flags & BKZ.MAX_TIME) and time.clock() - cputime_start >= params.max_time:
                break

        tracer.exit()
        self.trace = tracer.trace

    def tour(self, params, min_row=0, max_row=-1, tracer=dummy_tracer, top_level=False):
        """One BKZ loop over all indices.

        :param params: BKZ parameters
        :param min_row: start index ≥ 0
        :param max_row: last index ≤ n

        :returns: ``True`` if no change was made and ``False`` otherwise
        """
        if max_row == -1:
            max_row = self.A.nrows

        for kappa in range(min_row, max_row-2):
            block_size = min(params.block_size, max_row - kappa)
            self.svp_reduction(kappa, block_size, params, tracer, top_level=top_level)

        with tracer.context("gso"):
            self.M.update_gso() # TODO: we call this clean up, but we should be more clever about this

    def svp_preprocessing(self, kappa, block_size, param, tracer=dummy_tracer):

        with tracer.context("lll"):
            # make sure everything is somewhat sane, TODO this has a cost, when to drop it?
            self.lll_obj.size_reduction(kappa, kappa + block_size, kappa)
            # run LLL between kappa and kappa + block_size
            if self.M.get_current_slope(kappa, kappa + block_size) < -0.085:
                self.lll_obj(kappa, kappa, kappa + block_size, kappa)

        for preproc in param.strategies[block_size].preprocessing_block_sizes:
            prepar = param.__class__(block_size=preproc, strategies=param.strategies,
                                     flags=BKZ.GH_BND|BKZ.BOUNDED_LLL)
            self.tour(prepar, kappa, kappa + block_size, tracer=tracer)

    def svp_reduction(self, kappa, block_size, param, tracer=dummy_tracer, top_level=False):
        if top_level:
            # do a full LLL up to kappa + block_size
            with tracer.context("lll"):
                self.lll_obj(0, kappa, kappa+block_size, 0)

        remaining_probability, rerandomize = 1.0, False

        while remaining_probability > 1. - param.min_success_probability:
            with tracer.context("preprocessing"):
                if rerandomize:
                    with tracer.context("randomization"):
                        # make a copy of the local block to restore in case rerandomisation decreases quality
                        self.copy_block(kappa, block_size)
                        self.randomize_block(kappa+1, kappa+block_size,
                                             density=param.rerandomization_density, tracer=tracer)
                with tracer.context("reduction"):
                    self.svp_preprocessing(kappa, block_size, param, tracer=tracer)

            radius, expo = self.M.get_r_exp(kappa, kappa)
            radius *= self.lll_obj.delta

            if param.flags & BKZ.GH_BND and block_size > 30:
                root_det = self.M.get_root_det(kappa, kappa + block_size)
                radius, expo = adjust_radius_to_gh_bound(radius, expo, block_size, root_det, param.gh_factor)

            pruning = self.get_pruning(kappa, block_size, param, tracer)

            try:
                enum_obj = Enumeration(self.M)
                with tracer.context("enumeration",
                                    enum_obj=enum_obj,
                                    probability=pruning.expectation,
                                    full=block_size==param.block_size):
                    solution, max_dist = enum_obj.enumerate(kappa, kappa + block_size, radius, expo,
                                                            pruning=pruning.coefficients)[0]
                with tracer.context("postprocessing"):
                    self.svp_postprocessing(kappa, block_size, solution, tracer=tracer, top_level=top_level)
                    if rerandomize:
                        self.delete_copy_block(kappa, block_size, restore=False)
                rerandomize = False

            except EnumerationError:
                with tracer.context("postprocessing"):
                    if rerandomize:
                        # restore block, TODO don't do this unconditionally
                        self.delete_copy_block(kappa, block_size, restore=True)
                rerandomize = True

            remaining_probability *= (1 - pruning.expectation)

    def svp_postprocessing(self, kappa, block_size, solution, tracer, top_level):
        """Insert SVP solution into basis and LLL reduce.

        :param solution: coordinates of an SVP solution
        :param kappa: current index
        :param block_size: block size
        :param tracer: object for maintaining statistics

        """
        if solution is None:
            return

        nonzero_vectors = len([x for x in solution if x])
        lll_min = 0 if top_level else kappa

        first_nonzero_vector = None
        for i in range(block_size)[::-1]:
            if abs(solution[i]) == 1:
                first_nonzero_vector = i
                break

        if nonzero_vectors == 1:
            self.M.move_row(kappa + first_nonzero_vector, kappa)
            with tracer.context("lll"):
                self.lll_obj(lll_min, kappa, kappa+ 1, lll_min)

        elif first_nonzero_vector:
            # one coordinate is equal to ±1, linear dependency easy to fix
            d = self.M.d
            self.M.create_row()

            with self.M.row_ops(d, d + 1):
                for i in range(block_size):
                    self.M.row_addmul(d, kappa + i, solution[i])

            self.M.move_row(d, kappa)
            self.M.move_row(kappa + first_nonzero_vector + 1, d)
            self.M.remove_last_row()
            with tracer.context("lll"):
                self.lll_obj(lll_min, kappa, kappa + 1, lll_min)

        else:
            d = self.M.d
            self.M.create_row()

            with self.M.row_ops(d, d+1):
                for i in range(block_size):
                    self.M.row_addmul(d, kappa + i, solution[i])

            self.M.move_row(d, kappa)
            with tracer.context("lll"):
                self.lll_obj(lll_min, kappa, kappa + block_size + 1, lll_min)
            self.M.move_row(kappa + block_size, d)
            self.M.remove_last_row()

@begin.start(auto_convert=True)
def main(n=150, block_size=60, float_type="d", logq=40, verbose=False, seed=0xdeadbeef):
    print "= n: %3d, β: %2d, bits: %3d, float_type: %s, seed: 0x%08x ="%(n, block_size, logq, float_type, seed)
    print
    set_random_seed(seed)
    A = IntegerMatrix.random(n, "qary", k=n//2, bits=logq)
    A = LLL.reduction(A)

    params = BKZ.Param(block_size=block_size, max_loops=4, strategies=BKZ.DEFAULT_STRATEGY, flags=BKZ.MAX_LOOPS|BKZ.VERBOSE)
    bkz = BKZReduction(GSO.Mat(copy.copy(A), float_type=float_type))
    bkz(params)

    print bkz.trace

    bkz2 = BKZ2(GSO.Mat(copy.copy(A), float_type=float_type))
    bkz2(params)

    print bkz2.trace

    if verbose:
        print
        print bkz.trace.report()
