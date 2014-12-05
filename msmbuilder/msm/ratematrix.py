# Author: Robert McGibbon <rmcgibbo@gmail.com>
# Contributors:
# Copyright (c) 2014, Stanford University
# All rights reserved.

from __future__ import print_function, division
import time
import numpy as np
import scipy.linalg
import scipy.optimize
from six.moves import cStringIO

from ..base import BaseEstimator
from ..utils import list_of_1d, printoptions, experimental
from . import _ratematrix
from ._markovstatemodel import _transmat_mle_prinz
from .core import _MappingTransformMixin, _transition_counts


class ContinuousTimeMSM(BaseEstimator, _MappingTransformMixin):
    """Reversible first order master equation model

    This model fits a reversible continuous-time Markov model for labeled
    sequence data.

    .. warning::

        This model is currently (as of December 2, 2014) experimental, and may
        undergo significant changes or bugfixes in upcoming releases.

    Parameters
    ----------
    lag_time : int
        The lag time used to count the number of state to state transition
        events.
    prior_counts : float, optional
        Add a number of "pseudo counts" to each entry in the counts matrix.
        When prior_counts == 0 (default), the assigned transition
        probability between two states with no observed transitions will be
        zero, whereas when prior_counts > 0, even this unobserved transitions
        will be given nonzero probability.
    use_sparse : bool, default=True
        Attempt to find a sparse rate matrix.
    verbose : bool, default=False
        Verbosity level
    ftol : float, default=1e-6
        Iteration stops when the relative increase in the log-likelihood is less
        than this cutoff.

    Attributes
    ----------
    n_states_ : int
        The number of states
    ratemat_ : np.ndarray, shape=(n_states_, n_state_)
        The estimated state-to-state transition rates.
    transmat_ : np.ndarray, shape=(n_states_, n_state_)
        The estimated state-to-state transition probabilities over an interval
        of 1 time unit.
    timescales_ : array of shape (n-1,)
        Estimated relaxation timescales of the model.
    populations_ : np.ndarray, shape=(n_states_,)
        Estimated stationary probability distribution over the states.
    countsmat_ : array_like, shape = (n_states_, n_states_)
        Number of transition counts between states, at a time delay of ``lag_time``
        countsmat_[i, j] is counted during `fit()`.
    optimizer_state_ : object
        Contains information about the optimization termination.
    mapping_ : dict
        Mapping between "input" labels and internal state indices used by the
        counts and transition matrix for this Markov state model. Input states
        need not necessarily be integers in (0, ..., n_states_ - 1), for
        example. The semantics of ``mapping_[i] = j`` is that state ``i`` from
        the "input space" is represented by the index ``j`` in this MSM.
    theta_ : array of shape n*(n+1)/2 or shorter
        Optimized set of parameters for the model.
    information_ : np.ndarray, shape=(len(theta_), len(theta_))
        Approximate inverse of the hessian of the model log-likelihood
        evaluated at ``theta_``.
    inds_ : array of shape n*(n+1)/2 or shorter, or None
        For sparse parameterization, the indices of the non-zero elements of
        \theta.

    See Also
    --------
    MarkovStateModel : discrete-time analog
    """
    def __init__(self, lag_time=1, prior_counts=0, use_sparse=True,
                 verbose=False, ftol=1e-6):
        self.lag_time = lag_time
        self.prior_counts = prior_counts
        self.verbose = verbose
        self.use_sparse = use_sparse
        self.ftol = ftol

        self.inds_ = None
        self.theta_ = None
        self.ratemat_ = None
        self.transmat_ = None
        self.countsmat_ = None
        self.n_states_ = None
        self.optimizer_state_ = None
        self.mapping_ = None
        self.populations_ = None
        self.information_ = None
        self.loglikelihoods_ = None

    @experimental('ContinuousTimeMSM')
    def fit(self, sequences, y=None):
        sequences = list_of_1d(sequences)
        lag_time = int(self.lag_time)
        if lag_time < 1:
            raise ValueError('lag_time must be >= 1')
        countsmat, mapping = _transition_counts(sequences, lag_time)

        n_states = countsmat.shape[0]
        result, inds = self._optimize(countsmat + self.prior_counts)

        exptheta = np.exp(result.x)
        K = np.zeros((n_states, n_states))
        _ratematrix.build_ratemat(exptheta, n_states, inds, K, which='K')

        self.inds_ = inds
        self.theta_ = result.x
        self.ratemat_ = K
        self.transmat_ = scipy.linalg.expm(self.ratemat_)
        self.countsmat_ = countsmat
        self.n_states_ = n_states
        self.optimizer_state_ = result
        self.mapping_ = mapping
        self.populations_ = exptheta[-n_states:] / exptheta[-n_states:].sum()
        self.information_ = None
        self.timescales_ = -1 / np.sort(np.linalg.eigvals(self.ratemat_))[::-1][1:]

        return self

    def summarize(self):
        out = cStringIO()
        with printoptions(precision=4):
            print('n_states: %s' % self.n_states_, file=out)
            print(self.optimizer_state_.message, file=out)
            print('ratemat\n', self.ratemat_, file=out)
            print('transmat\n', self.transmat_, file=out)
            print('populations\n', self.populations_, file=out)
            print('timescales\n', self.timescales_, file=out)
            print('uncertainty pi\n', self.uncertainty_pi(), file=out)
            print('uncertainty timescales\n', self.uncertainty_timescales(), file=out)

        return out.getvalue()

    def _optimize(self, countsmat):
        n = countsmat.shape[0]
        nc2 = int(n*(n-1)/2)
        theta_cutoff = np.log(1e-8)
        loglikelihoods = []

        theta0 = self.initial_guess(countsmat)
        lag_time = float(self.lag_time)

        options = {
            'iprint': 0 if self.verbose else -1,
            'ftol': self.ftol,
            #'gtol': 1e-10
        }

        def objective(theta, inds):
            start = time.time()
            f, g = _ratematrix.loglikelihood(
                theta, countsmat, n, inds, lag_time)

            loglikelihoods.append((f, start, len(theta)))
            return -f, -g

        # this bound prevents the stationary probability for any state
        # from going below exp(-20), which helps avoid NaNs, since the
        # rate matrix involves terms like pi_i / pi_j, which get iffy
        # numerically as the populations go too close to zero. We also
        # prevent the S_ijs from getting similarly small, since in the next
        # optimization step using the sparse parameterizetion, they can get
        # truncated.
        bounds0 = [(-20, None)]*nc2 + [(-20, 0)]*n
        inds0 = None
        options0 = dict(options, maxiter=max(n//10, 25)) if self.use_sparse else options
        result0 = scipy.optimize.minimize(
            fun=objective, x0=theta0, method='L-BFGS-B', jac=True,
            bounds=bounds0, args=(inds0,), options=options0)

        # now, try rerunning the optimization with theta restricted to only
        # the dominant elements -- try zeroing out the elements that are too
        # small.
        inds1 = np.concatenate((
            np.where(result0.x[:nc2] > theta_cutoff)[0], nc2 + np.arange(n)))

        if (len(inds1) == nc2 + n) or (not self.use_sparse):
            value = (result0, inds0)
        else:
            bounds1 = [bounds0[i] for i in inds1]
            result1 = scipy.optimize.minimize(
                fun=objective, x0=result0.x[inds1], method='L-BFGS-B', jac=True,
                bounds=bounds1, args=(inds1,), options=options)

            if result1.fun < result0.fun:
                if self.verbose:
                    print('[ContinuousTimeMSM] %d rates pegged to zero' %
                          (nc2 + n - len(inds1)))
                value = (result1, inds1)
            else:
                if self.verbose:
                    print('[ContinuousTimeMSM] No rates pegged to zero')
                value = (result0, inds0)

        self.loglikelihoods_ = np.array(loglikelihoods)
        return value

    def uncertainty_K(self):
        """Estimate of the element-wise asymptotic standard deviation
        in the rate matrix
        """
        if self.information_ is None:
            self._build_information()

        sigma_K = _ratematrix.sigma_K(
            self.information_, theta=self.theta_, n=self.n_states_,
            inds=self.inds_)
        return sigma_K

    def uncertainty_pi(self):
        """Estimate of the element-wise asymptotic standard deviation
        in the stationary distribution.
        """
        if self.information_ is None:
            self._build_information()

        sigma_pi = _ratematrix.sigma_pi(
            self.information_, theta=self.theta_, n=self.n_states_,
            inds=self.inds_)
        return sigma_pi

    def uncertainty_timescales(self):
        """Estimate of the element-wise asymptotic standard deviation
        in the model relaxation timescales.
        """
        if self.information_ is None:
            self._build_information()

        sigma_timescales = _ratematrix.sigma_timescales(
            self.information_, theta=self.theta_, n=self.n_states_,
            inds=self.inds_)
        return sigma_timescales

    def initial_guess(self, countsmat):
        # C = 0.5 * (countsmat + countsmat.T) + self.prior_counts
        # pi = C.sum(axis=0) / C.sum(dtype=float)
        # transmat = C.astype(float) / C.sum(axis=1)[:, None]
        transmat, pi = _transmat_mle_prinz(countsmat + self.prior_counts)

        K = np.real(scipy.linalg.logm(transmat))
        S = np.multiply(np.sqrt(np.outer(pi, 1/pi)), K)
        sflat = np.maximum(S[np.triu_indices_from(countsmat, k=1)], 1e-10)
        theta0 = np.concatenate((np.maximum(-19, np.log(sflat)), np.log(pi)))

        return theta0

    def _build_information(self):
        """Build the inverse of hessian of the log likelihood at theta_
        """
        lag_time = float(self.lag_time)

        hessian = _ratematrix.hessian(
            self.theta_, self.countsmat_, self.n_states_, t=lag_time,
            inds=self.inds_)

        self.information_ = scipy.linalg.pinv(-hessian)
