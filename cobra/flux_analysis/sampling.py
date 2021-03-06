# -*- coding: utf-8 -*-

"""Module implementing flux sampling for cobra models.

New samplers should derive from the abstract `HRSampler` class
where possible to provide a uniform interface.
"""

from __future__ import absolute_import, division

import ctypes
from multiprocessing import Array, Pool
from time import time

import numpy as np
import pandas
from cobra.solvers import get_solver_name, solver_dict
from cobra.util import create_stoichiometric_array, nullspace

BTOL = np.finfo(np.float32).eps
"""The tolerance used for checking bounds feasibility."""

FTOL = BTOL
"""The tolerance used for checking equalities feasibility."""

bad_x = None
bad_delta = None
bad_alpha = None


# Has to be declared outside of class to be used for multiprocessing :(
def _step(sampler, x, delta, fraction=None):
    """Samples a new feasible point from the point `x` in direction `delta`."""
    # delta = delta / np.sqrt((delta * delta).sum())
    nonzero = np.abs(delta) > 0.0
    alphas = ((1.0 - BTOL) * sampler.bounds - x)[:, nonzero]
    alphas = (alphas / delta[nonzero]).flatten()
    alpha_range = (alphas[alphas > 0.0].min(), alphas[alphas <= 0.0].max())
    if fraction:
        alpha = alpha_range[0] + fraction * (alpha_range[1] - alpha_range[0])
    else:
        alpha = np.random.uniform(alpha_range[0], alpha_range[1])
    p = x + alpha * delta

    # Numerical instabilities may cause bounds invalidation
    # reset sampler and sample from one of the original warmup directions
    # if that occurs
    if np.any(sampler._bounds_dist(p) < -BTOL):
        newdir = sampler.warmup[np.random.randint(sampler.n_warmup)]
        return _step(sampler, sampler.center, newdir - sampler.center)
    return p


class HRSampler(object):
    """The abstract base class for hit-and-run samplers.

    Parameters
    ----------
    model : cobra.Model
        The cobra model from which to generate samples.
    thinning : int
        The thinning factor of the generated sampling chain. A thinning of 10
        means samples are returned every 10 steps.

    Attributes
    ----------
    model : cobra.Model
        The cobra model from which the samples get generated.
    thinning : int
        The currently used thinning factor.
    n_samples : int
        The total number of samples that have been generated by this
        sampler instance.
    bounds : a numpy matrix
        The matrix has 2 rows, the first containing the lower flux bounds
        in its columns and the second the upper flux bounds.
    warmup : a numpy matrix
        A matrix of with as many columns as reactions in the model and more
        than 3 rows containing a warmup sample in each row. None if no warmup
        points have been generated yet.
    seed : positive integer, optional
        Sets the random number seed. Initialized to the current time stamp if
        None.
    """

    def __init__(self, model, thinning, seed=None):
        self.model = model
        self.thinning = thinning
        self.n_samples = 0
        self.S = create_stoichiometric_array(model, array_type='dense')
        self.NS = nullspace(self.S)
        self.bounds = np.array([[r.lower_bound, r.upper_bound]
                               for r in model.reactions]).T
        self.fixed = np.diff(self.bounds, axis=0).flatten() < 2 * BTOL
        self.warmup = None
        if seed is None:
            self._seed = int(time())
        else:
            self._seed = seed
        # Avoid overflow
        self._seed = self._seed % np.iinfo(np.int32).max

    def generate_fva_warmup(self, solver=None, **solver_args):
        """Generates the warmup points for the sampler.

        Generates warmup points by setting each flux as the sole objective
        and minimizing/maximizing it.

        Parameters
        ----------
        solver : str or cobra solver interface, optional
            The solver used for the arising LP problems.
        **solver_args
            Additional arguments passed to the solver.
        """

        solver = solver_dict[get_solver_name() if solver is None else solver]
        lp = solver.create_problem(self.model)
        for i, r in enumerate(self.model.reactions):
            solver.change_variable_objective(lp, i, 0.0)

        self.n_warmup = 0
        self.warmup = np.zeros((2 * len(self.model.reactions),
                                len(self.model.reactions)))

        for sense in ("minimize", "maximize"):
            for i, r in enumerate(self.model.reactions):
                # Omit fixed reactions
                if self.fixed[i]:
                    pass
                solver.change_variable_objective(lp, i, 1.0)
                solver.solve_problem(lp, objective_sense=sense, **solver_args)
                sol = solver.format_solution(lp, self.model).x
                if not sol:
                    pass
                # some solvers do not enforce bounds too much -> we reconstrain
                sol = np.maximum(sol, self.bounds[0, ])
                sol = np.minimum(sol, self.bounds[1, ])
                self.warmup[self.n_warmup, ] = sol
                self.n_warmup += 1
                # revert objective
                solver.change_variable_objective(lp, i, 0.)
        # Shrink warmup points to measure
        self.warmup = self.warmup[0:self.n_warmup, ]

    def _reproject(self, p):
        """Reprojects a point into the feasibility region"""
        return self.NS.dot(self.NS.T.dot(p))

    def _bounds_dist(self, p):
        """Get the lower and upper bound distances. Negative is bad."""
        lb_dist = (p - self.bounds[0, ]).min()
        ub_dist = (self.bounds[1, ] - p).min()
        return np.array([lb_dist, ub_dist])

    def sample(self, n):
        """Abstract sampling function.

        Should be overwritten by child classes.
        """
        pass

    def batch(self, batch_size, batch_num):
        """Generates a batch generator.

        This is useful to generate n batches of m samples each.

        Parameters
        ----------
        batch_size : int
            The number of samples contained in each batch (m).
        batch_num : int
            The number of batches in the generator (n).

        Yields
        ------
        numpy.matrix
            A matrix containing with dimensions (batch_size x n_r) containing
            a valid flux sample for a total of n_r reactions in each row.
        """
        for i in range(batch_num):
            yield self.sample(batch_size)

    def validate(self, samples):
        """Validates a set of samples for equality and inequality feasibility.

        Can be used to check whether the generated samples and warmup points
        are feasible.

        Parameters
        ----------
        samples : numpy.matrix
            Must be of dimension (n_samples x n_reactions). Contains the
            samples to be validated.

        Returns
        -------
        numpy.array
            A one-dimensional numpy array of length containing
            a code of 1 to 3 letters denoting the validation result:

            - 'v' means feasible in bounds and equality constraints
            - 'l' means a lower bound violation
            - 'u' means a lower bound validation
            - 'e' means and equality constraint violation
        """
        samples = np.atleast_2d(samples)
        feasibility = np.abs(self.S.dot(samples.T))
        feasibility = feasibility.max(axis=0)
        lb_error = (samples - self.bounds[0, ]).min(axis=1)
        ub_error = (self.bounds[1, ] - samples).min(axis=1)
        valid = (feasibility < FTOL) & (lb_error > -BTOL) & (ub_error > -BTOL)
        codes = np.repeat("", valid.shape[0]).astype(np.dtype((str, 3)))
        codes[valid] = "v"
        codes[lb_error <= -BTOL] = np.char.add(codes[lb_error <= -BTOL], "l")
        codes[ub_error <= -BTOL] = np.char.add(codes[ub_error <= -BTOL], "u")
        codes[feasibility > FTOL] = np.char.add(codes[feasibility > FTOL], "e")
        return codes


class ARCHSampler(HRSampler):
    """Artificial Centering Hit-and-Run sampler.

    A sampler with low memory foot print and good convergence.

    Parameters
    ----------
    model : a cobra model
        The cobra model from which to generate samples.
    thinning : int, optional
        The thinning factor of the generated sampling chain. A thinning of 10
        means samples are returned every 10 steps.
    solver : str or cobra solver interface, optional
        The solver used for the arising LP problems during warmup point
        generation.
    seed : positive integer, optional
        Sets the random number seed. Initialized to the current time stamp if
        None.
    **solver_args
        Additional arguments passed to the solver.

    Attributes
    ----------
    model : a cobra model
        The cobra model from which the samples get generated.
    thinning : int
        The currently used thinning factor.
    n_samples : int
        The total number of samples that have been generated by this
        sampler instance.
    bounds : a numpy matrix
        The matrix has 2 rows, the first containing the lower flux bounds
        in its columns and the second the upper flux bounds.
    warmup : a numpy matrix
        A matrix of with as many columns as reactions in the model and more
        than 3 rows containing a warmup sample in each row.
    prev : numpy array
        The current/last flux sample generated.
    center : numpy array
        The center of the sampling space as estimated by the mean of all
        previously generated samples.

    Notes
    -----
    ARCH generates samples by choosing new directions from the sampling space's
    center and the warmup points. The implementation used here is the same
    as in the Matlab Cobra Toolbox [2]_ and uses only the initial warmup points
    to generate new directions and not any other previous iterates. This
    usually gives better mixing since the startup points are chosen to span
    the space in a wide manner. This also makes the generated sampling chain
    quasi-markovian since the center converges rapidly.

    References
    ----------
    .. [1] Direction Choice for Accelerated Convergence in Hit-and-Run Sampling
       David E. Kaufman Robert L. Smith
       Operations Research 199846:1 , 84-95
       https://doi.org/10.1287/opre.46.1.84
    .. [2] https://github.com/opencobra/cobratoolbox
    """

    def __init__(self, model, thinning=100, solver=None,
                 seed=None, **solver_kwargs):
        super(ARCHSampler, self).__init__(model, thinning, seed=seed)
        self.generate_fva_warmup(solver, **solver_kwargs)
        self.prev = self.center = self.warmup.mean(axis=0)
        np.random.seed(self._seed)

    def __single_iteration(self):
        pi = np.random.randint(self.n_warmup)
        # mix in the original warmup points to not get stuck
        p = self.warmup[pi, ]
        delta = p - self.center
        self.prev = _step(self, self.prev, delta)
        if (self.n_samples * self.thinning % 1000 == 0):
            self.prev = self._reproject(self.prev)
        self.center = (self.n_samples * self.center + self.prev) / (
                       self.n_samples + 1)
        self.n_samples += 1

    def sample(self, n):
        """Generate a set of samples.

        This is the basic sampling function for all hit-and-run samplers.

        Parameters
        ---------
        n : int
            The number of samples that are generated at once.

        Returns
        -------
        numpy.matrix
            Returns a matrix with `n` rows, each containing a flux sample.

        Notes
        -----
        Performance of this function linearly depends on the number
        of reactions in your model and the thinning factor.
        """
        samples = np.zeros((n, len(self.model.reactions)))
        for i in range(1, self.thinning * n + 1):
            self.__single_iteration()
            if (self.n_samples * self.thinning % 1000 == 0):
                self.prev = self.NS.dot(self.NS.T.dot(self.prev))
            if i % self.thinning == 0:
                samples[i//self.thinning - 1, ] = self.prev
        return samples


# Unfortunately this has to be outside the class to be usable with
# multiprocessing :()
def _sample_chain(args):
    """samples a single chain for OptGPSampler.

    center and n_samples are updated locally and forgotten afterwards.
    """
    sampler, n, idx = args       # has to be this way to work in Python 2.7
    center = sampler.center
    np.random.seed((sampler._seed + idx) % np.iinfo(np.int32).max)
    prev = sampler.warmup[np.random.randint(sampler.n_warmup), ]
    prev = _step(sampler, center, prev - center, 0.95)
    n_samples = max(sampler.n_samples, 1)
    samples = np.zeros((n, center.shape[0]))

    for i in range(1, sampler.thinning * n + 1):
        pi = np.random.randint(sampler.n_warmup)
        p = sampler.warmup[pi, ]

        delta = p - center
        prev = _step(sampler, prev, delta)
        if (n_samples * sampler.thinning % 1000 == 0):
            prev = sampler._reproject(prev)
        if i % sampler.thinning == 0:
            samples[i//sampler.thinning - 1, ] = prev
            center = (n_samples * center + prev) / (n_samples + 1)
            n_samples += 1

    return samples


class OptGPSampler(HRSampler):
    """A parallel optimized sampler.

    A parallel sampler with fast convergence and parallel execution. See [1]_
    for details.

    Parameters
    ----------
    model : cobra.Model
        The cobra model from which to generate samples.
    processes: int
        The number of processes used during sampling.
    thinning : int, optional
        The thinning factor of the generated sampling chain. A thinning of 10
        means samples are returned every 10 steps.
    solver : str or cobra solver interface, optional
        The solver used for the arising LP problems during warmup point
        generation.
    seed : positive integer, optional
        Sets the random number seed. Initialized to the current time stamp if
        None.
    **solver_args
        Additional arguments passed to the solver.

    Attributes
    ----------
    model : cobra.Model
        The cobra model from which the samples get generated.
    thinning : int
        The currently used thinning factor.
    np : int
        The number of processes used by the sampler.
    n_samples : int
        The total number of samples that have been generated by this
        sampler instance.
    bounds : numpy.matrix
        The matrix has 2 rows, the first containing the lower flux bounds
        in its columns and the second the upper flux bounds.
    warmup : numpy.matrix
        A matrix of with as many columns as reactions in the model and more
        than 3 rows containing a warmup sample in each row.
    prev : numpy.array
        The current/last flux sample generated.
    center : numpy.array
        The center of the sampling space as estimated by the mean of all
        previously generated samples.

    Notes
    -----
    The sampler is very similar to artificial centering where each process
    samples its own chain. Initial points are chosen randomly from the warmup
    points followed by a linear transformation that pulls the points towards
    the a little bit towards the center of the sampling space.

    If the number of processes used is larger than one the requested
    number of samples is adjusted to the smallest multiple of the number of
    processes larger than the requested sample number. For instance, if you
    have 3 processes and request 8 samples you will receive 9.

    References
    ----------
    .. [1] Megchelenbrink W, Huynen M, Marchiori E (2014)
       optGpSampler: An Improved Tool for Uniformly Sampling the Solution-Space
       of Genome-Scale Metabolic Networks.
       PLoS ONE 9(2): e86587.
       https://doi.org/10.1371/journal.pone.0086587
    """

    def __init__(self, model, processes, thinning=100, solver=None,
                 seed=None, **solver_kwargs):
        super(OptGPSampler, self).__init__(model, thinning, seed=seed)
        self.generate_fva_warmup(solver, **solver_kwargs)
        self.np = processes

        # This maps our saved center into shared memory,
        # meaning they are synchronized across processes
        shared_center = Array(ctypes.c_double, len(model.reactions))
        self.center = np.frombuffer(shared_center.get_obj())
        # Has to be like this because we want a copy
        self.center[:] = self.warmup.mean(axis=0)

    def sample(self, n):
        """Generate a set of samples.

        This is the basic sampling function for all hit-and-run samplers.

        Paramters
        ---------
        n : int
            The minimum number of samples that are generated at once
            (see Notes).

        Returns
        -------
        numpy.matrix
            Returns a matrix with `n` rows, each containing a flux sample.

        Notes
        -----
        Performance of this function linearly depends on the number
        of reactions in your model and the thinning factor.

        If the number of processes is larger than one, computation is split
        across as the CPUs of your machine. This may shorten computation time.
        However, there is also overhead in setting up parallel computation so
        we recommend to calculate large numbers of samples at once
        (`n` > 1000).
        """
        if self.np > 1:
            n_process = np.ceil(n / self.np).astype(int)
            n = n_process * self.np
            # No with statement or starmap here since Python 2.x
            # does not support it :(
            mp = Pool(self.np)
            chains = mp.map(
                _sample_chain,
                zip([self] * self.np, [n_process] * self.np, range(self.np))
                )
            chains = np.vstack(chains)
            mp.terminate()
        else:
            chains = _sample_chain((self, n, 0))

        # Update the global center
        self.center = (self.n_samples * self.center +
                       n * np.atleast_2d(chains).mean(axis=0)) / (
                       self.n_samples + n)
        self.n_samples += n
        return chains

    # Models can be large so don't pass them around during multiprocessing
    def __getstate__(self):
        d = dict(self.__dict__)
        del d['model']
        return d


def sample(model, n, method="optgp", processes=1, seed=None,
           solver=None, **solver_kwargs):
    """Samples valid flux distribution from a cobra model.

    The function samples valid flux distributions from a cobra model.
    Currently we support two methods:

    1. 'optgp' (default) which uses the OptGPSampler that supports parallel
        sampling [1]_. Requires large numbers of samples to be performant
        (n < 1000). For smaller samples 'arch' might be better suited.

    or

    2. 'arch' which uses artificial centering hit-and-run. This is a single
       process method with good convergence [2]_.

    Parameters
    ----------
    model : cobra.Model
        The model from which to sample flux distributions.
    n : int
        The number of samples to obtain. When using 'optgp' this must be a
        multiple of `processes`, otherwise a larger number of samples will be
        returned.
    method : str, optional
        The sampling algorithm to use.
    processes : int, optional
        Only used for 'optgp'. The number of processes used to generate
        samples.
    solver : str or cobra solver interface, optional
        The solver used for the arising LP problems during warmup point
        generation.
    seed : positive integer, optional
        The random number seed to be used. Initialized to current time stamp
        if None.
    **solver_args
        Additional arguments passed to the solver.

    Returns
    -------
    pandas.DataFrame
        The generated flux samples. Each row corresponds to a sample of the
        fluxes and the columns are the reactions.

    Notes
    -----
    Currently, cobrapy does not support the definition of complex constraints
    for a model. If you want to add additional constraints those have to be
    implemented in your model with mock reactions.

    References
    ----------
    .. [1] Megchelenbrink W, Huynen M, Marchiori E (2014)
       optGpSampler: An Improved Tool for Uniformly Sampling the Solution-Space
       of Genome-Scale Metabolic Networks.
       PLoS ONE 9(2): e86587.
    .. [2] Direction Choice for Accelerated Convergence in Hit-and-Run Sampling
       David E. Kaufman Robert L. Smith
       Operations Research 199846:1 , 84-95
    """
    if method == "optgp":
        sampler = OptGPSampler(model, processes, solver=solver, seed=seed,
                               **solver_kwargs)
    elif method == "arch":
        sampler = ARCHSampler(model, solver=solver, seed=seed, **solver_kwargs)
    else:
        raise ValueError("method must be 'optgp' or 'arch'!")

    return pandas.DataFrame(columns=[rxn.id for rxn in model.reactions],
                            data=sampler.sample(n))
