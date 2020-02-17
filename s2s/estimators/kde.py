import numpy as np
from typing import List

from sklearn.model_selection import GridSearchCV
from sklearn.neighbors import KernelDensity

from s2s.utils import show

__author__ = 'Steve James and George Konidaris'


class KernelDensityEstimator:

    def __init__(self, mask: List[int]):
        """
        Create a new estimator
        :param mask: the state variables we care about
        """
        self._mask = mask
        self._kde: KernelDensity = None

    def fit(self, X: np.ndarray, verbose=False, **kwargs) -> None:
        """
        Fit the data to the effect estimator using a grid search for the hyperparameters with cross-validation
        :param X: the data
        :param verbose: the verbosity level
        """
        if kwargs.get('masked', False):
            data = X  # already been masked
        else:
            data = X[:, self.mask]
        bandwidth_range = kwargs.get('effect_bandwidth_range', np.arange(0.001, 0.1, 0.001))
        params = {'bandwidth': bandwidth_range}
        grid = GridSearchCV(KernelDensity(kernel='gaussian'), params, cv=3)
        grid.fit(data)
        show("Best bandwidth hyperparameter: {}".format(grid.best_params_['bandwidth']), verbose)
        self._kde = grid.best_estimator_

    @property
    def mask(self) -> List[int]:
        """
        Get the effect mask
        """
        return self._mask

    def sample(self, n_samples=100):
        return self._kde.sample(n_samples)

    @staticmethod
    def _extract_remaining(a, b):
        """
          Return the elements of a that are not in b.
          Return two nd-arrays, the first listing these elements,
          the second listing their indices.
        """
        new_vars = []
        new_indices = []

        for pos in range(0, len(a)):
            val = a[pos]
            if not (val in b):
                new_vars.append(val)
                new_indices.append(pos)

        return np.array(new_vars), np.array(new_indices)

    def integrate_out(self,
                      variable_list,
                      **kwargs):
        """
        Given a distribution p(s) and a list of variables, return a new
        distribution equal to p with those variables marginalized out.
        """
        new_vars = list()
        new_indices = list()

        for pos in range(0, len(self.mask)):
            val = self.mask[pos]
            if val not in variable_list:
                new_vars.append(val)
                new_indices.append(pos)
        n_samples = kwargs.get('estimator_samples', 100)
        new_samples = self.sample(n_samples)[:, new_indices]
        kde = KernelDensityEstimator(mask=new_vars)
        kwargs['masked'] = True  # the data has already been masked
        kde.fit(new_samples, **kwargs)
        return kde
