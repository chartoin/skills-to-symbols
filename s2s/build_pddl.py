import multiprocessing
from functools import partial
from typing import List, Tuple, Dict
from warnings import warn

import gym
import itertools
import pandas as pd

import numpy as np
from sklearn.metrics import silhouette_score

from s2s.estimators.svc import SupportVectorClassifier
from s2s.learned_operator import LearnedOperator
from s2s.estimators.kde import KernelDensityEstimator
from s2s.pddl.operator import Operator
from s2s.pddl.predicate import Predicate
from s2s.pddl.unique_list import UniquePredicateList
from s2s.utils import show, pd2np, run_parallel

__author__ = 'Steve James and George Konidaris'


def _equal_dists(x: KernelDensityEstimator, y: KernelDensityEstimator) -> bool:
    """
    Determine whether two effect distributions are equal. Here this is approximated by drawing samples and computing the
    silhouette score. If its absolute value is less than 0.2, the two distributions are equal.
    :param x: the first distribution
    :param y: the second distribution
    """
    if set(x.mask) != set(y.mask):
        return False
    n_samples = 100
    X = x.sample(n_samples)
    Y = y.sample(n_samples)

    data = np.concatenate((X, Y))
    labels = [0] * n_samples + [1] * n_samples
    overlap = silhouette_score(data, labels)

    threshold = 0.2
    return -threshold <= overlap <= threshold


def build_pddl(env: gym.Env, transition_data: pd.DataFrame, operators: List[LearnedOperator], verbose=False,
               **kwargs) -> Tuple[UniquePredicateList, List[Operator]]:
    """
    Given the learned preconditions and effects, generate a valid PDDL representation
    :param env: teh domain
    :param transition_data: the transition data
    :param operators: the learned operators
    :param verbose: the verbosity level
    :return: the predicates and PDDL operators
    """
    dist_comparator = kwargs.get('dist_comparator', _equal_dists)
    vocabulary = UniquePredicateList(dist_comparator)
    # Factorise the state space: see JAIR paper for more
    show("Factorising state space...", verbose)
    n_dims = env.observation_space.shape[-1]
    factors = _factorise(operators, n_dims, verbose=verbose)

    # generate a distribution over start states
    show("Generating start state symbols...", verbose)
    start_distribution = _generate_start_distribution(transition_data, verbose=verbose, **kwargs)

    # integrate all possible combinations of factors out of the start state distribution
    start_factors = _extract_factors(start_distribution.mask, factors)
    for length in range(1, len(start_factors)):
        for subset in itertools.combinations(start_factors, length):
            new_dist = start_distribution.integrate_out(np.concatenate(subset))
            vocabulary.append(new_dist)

    show("Start position generated {} propositions".format(len(vocabulary)), verbose)
    show("Generating propositions...", verbose)

    # get propositions directly from effects
    operator_predicates = _generate_vocabulary(vocabulary, operators, factors, verbose=verbose)
    show("Generating full PDDL...", verbose)

    n_procs = kwargs.get('n_processes', 4)  # n_procs = multiprocessing.cpu_count()
    # do it in parallel!  # TODO VERIFY NO ISSUES!
    show("Running on {} CPUs".format(n_procs), verbose)
    splits = np.array_split(operators, n_procs)
    functions = [
        partial(_build_pddl_operators, env, factors, splits[i], vocabulary, operator_predicates, verbose, **kwargs)
        for i in range(n_procs)]
    schemata = sum(run_parallel(functions), [])

    return vocabulary, schemata


def _build_pddl_operators(env: gym.Env, factors: List[List[int]], operators: List[LearnedOperator],
                          vocabulary: UniquePredicateList,
                          operator_predicates: Dict[Tuple[LearnedOperator, int], List[Predicate]],
                          verbose=False, **kwargs):
    """
    Generate the high-level PDDL operators, given the vocabulary and learned operators
    :param env: the domain
    :param factors: the factorisation of the state space
    :param operators: the earned operators
    :param vocabulary: the vocabulary
    :param operator_predicates: a mapping from learned operator and probabilistic effect to the predicates in the vocab
    :param verbose: the verbosity level
    :return: a list of PDDL operators
    """
    schemata = list()
    for i, operator in enumerate(operators):
        show("Processing {}/{} operators".format(i, len(operators)), verbose)
        precondition = operator.precondition
        pre_factors_indices = _mask_to_factors(precondition.mask, factors)
        pddl_operators = _build_pddl_operator(env, factors, operator, pre_factors_indices, vocabulary,
                                              operator_predicates,
                                              **kwargs)
        schemata.extend(pddl_operators)
    return schemata


def _probability_in_precondition(estimators: List[Predicate], precondition: SupportVectorClassifier, verbose=False,
                                 **kwargs) -> float:
    """
    Draw samples from the estimators and feed to the precondition. Take the average result
    :param estimators: the list of estimators
    :param precondition: the precondition
    :param verbose: the verbosity level
    :return: the probability of samples drawn from the estimators being in the precondition
    """

    mask = list()
    for predicate in estimators:
        mask.extend(predicate.mask)

    if not set(mask).issuperset(set(precondition.mask)):
        return 0

    keep_indices = [i for i in range(len(mask)) if mask[i] in precondition.mask]

    # Bail if no overlap.
    if len(keep_indices) == 0:
        return 0

    # TODO:
    n_samples = kwargs.get('estimator_samples', 100)
    samples = np.hstack([predicate.sample(n_samples) for predicate in estimators])
    samples = samples[:, keep_indices]

    # if the estimators are a subset of the precondition, randomly add data to fill in
    add_list = []
    for m in precondition.mask:
        if m not in mask:
            add_list.append(m)
    if len(add_list) > 0:
        show("Must randomly fill in data from {} to intersect with precondition".format(add_list), verbose)
        raise NotImplementedError

    total_mask = np.array(mask)[keep_indices]
    s_prob = 0
    for pos in range(n_samples):
        point = samples[pos, :]
        t_point = np.zeros([np.max(total_mask) + 1])
        t_point[total_mask] = point
        s_prob += precondition.probability(t_point)
    return s_prob / n_samples

    # add_list = []
    # for m in conditional_symbol.mask:
    #     if m not in self._mask:
    #         add_list.append(m)
    #
    # total_mask = self._mask[keep_indices]
    #
    # if len(add_list) > 0:
    #
    #     if not allow_fill_in:
    #         # not allowed to fill in data (e.g. ames et al)
    #         return 0
    #     if fill_in is not None:
    #         uniform = np.array([list(fill_in[add_list])] * self._mc_samples)
    #     else:
    #         uniform = np.random.uniform(0.0, 1.0, size=[self._mc_samples, len(add_list)])
    #     mc_new = np.zeros([self._mc_samples, len(keep_indices) + len(add_list)])
    #     mc_new[:, 0:len(keep_indices)] = mc_samples
    #     mc_new[:, len(keep_indices):] = uniform
    #     mc_samples = mc_new
    #     total_mask = np.concatenate((total_mask, np.array(add_list)))


def _build_pddl_operator(env: gym.Env, factors: List[List[int]], operator: LearnedOperator,
                         precondition_factor_indices: List[int], vocabulary: UniquePredicateList,
                         operator_predicates: Dict[Tuple[LearnedOperator, int], List[Predicate]],
                         verbose=False, **kwargs) -> List[Operator]:
    """
    Generate the PDDL representation for the given operator. There may be more than one due to disjunctive preconditions
    :param env: the domain
    :param factors: the factorisation of the state space
    :param operators: the learned operator
    :param precondition_factor_indices: the indices of each factor that should be considered
    :param vocabulary: the vocabulary
    :param operator_predicates: a mapping from learned operator and probabilistic effect to the predicates in the vocab
    :param verbose: the verbosity level
    """

    pddl_operators = list()

    candidates = list()  # candidates are all possible propositions that we need to consider

    # Get all symbols whose mask matches the correct factors
    for idx in precondition_factor_indices:
        s_list = []
        for predicate in vocabulary:
            mask = factors[idx]
            if set(predicate.mask) == set(mask):
                s_list.append(predicate)
        candidates.append(s_list)

    high_threshold = kwargs.get('high_threshold', 0.95)
    low_threshold = kwargs.get('low_threshold', 0.1)

    # when intersecting propositions with preconditions allow for the effects to be a subspace of the precondition
    # (and have the missing variables randomly sampled)
    allow_fill_in = kwargs.get('allow_fill_in', False)

    # try out all possible combinations!
    combinations = list(itertools.product(*candidates))
    show("Searching through {} candidates...".format(len(combinations)), verbose)
    for count, candidates in enumerate(combinations):
        show("Checking candidate {}".format(count), verbose)
        if _masks_overlap(candidates):
            # This should never happen, but putting a check to make sure
            warn("Overlapping candidates in PDDL building!")
            continue
        # get the precondition masks from the candidates. Make sure sorted to avoid bugs!
        precondition_masks = sorted(
            list(itertools.chain.from_iterable([proposition.mask for proposition in candidates])))

        # if (not allow_fill_in and not np.array_equal(precondition_masks, operator.precondition.mask)) or (
        #         allow_fill_in and not set(precondition_masks).issubset(operator.precondition.mask)):
        #     #  effect mask does not match precondition!
        #     continue

        # probability of propositions matching classifier
        precondition_prob = _probability_in_precondition(candidates, operator.precondition)
        if precondition_prob > low_threshold:
            # we found a match!
            precondition_prob = round(precondition_prob, 3)  # make look nice
            pddl_operator = Operator(operator)
            pddl_operator.add_preconditions(candidates)

            remaining_probability = 1
            if precondition_prob < high_threshold:
                remaining_probability = precondition_prob
                pddl_operator.add_effect(Predicate.not_failed().negate(),
                                         1 - precondition_prob)  # add failure condition

            for i, (outcome_prob, effect, reward_estimator) in enumerate(operator.outcomes()):
                prob = outcome_prob * remaining_probability
                reward = None if reward_estimator is None else reward_estimator.expected_reward(env, effect, **kwargs)
                positive_effects = operator_predicates[(operator, i)]

                # Negative effects.
                # Filter: only symbols with factors that overlap the effects mask.
                negative_effects = [x for x in vocabulary if set(x.mask).issubset(set(effect.mask))]
                # Filter: remove positive effects.
                negative_effects = [x for x in negative_effects if x not in positive_effects]
                # Filter: in the precondition - only if explicitly mentioned.
                negative_effects = [x for x in negative_effects if
                                    not (set(x.mask).issubset(precondition_masks) and (x not in candidates))]
                negative_effects = [x.negate() for x in negative_effects]
                pddl_operator.add_effect(positive_effects + negative_effects, prob, reward)
            pddl_operators.append(pddl_operator)
    return pddl_operators


def _masks_overlap(propositions: List[Predicate]):
    """
    Check if a set of propositions have overlapping masks
    :param propositions: the set of propositions
    :return: whether their masks overlap
    """
    variables = np.hstack([symbol.mask for symbol in propositions])
    return np.unique(variables).size != len(variables)


def _generate_vocabulary(vocabulary: UniquePredicateList, operators: List[LearnedOperator], factors: List[List[int]],
                         verbose=False, **kwargs) -> Dict[Tuple[LearnedOperator, int], List[Predicate]]:
    """
    Generate a vocabulary for the PDDL. This includes every possible proposition that could ever be required.
    :param vocabulary: the existing vocabulary of predicates
    :param operators: the learned operators
    :param factors: the factors
    :param verbose: the verbosity level
    :return: a mapping from learned operator and probabilistic effect to the predicates in the vocabulary
    """
    # Process each option's effect sets.
    # map from (operator, probabilistic effect) -> predicates
    operator_predicates: Dict[Tuple[LearnedOperator, int], List[Predicate]] = {}
    for operator in operators:
        for i, (_, effect, _) in enumerate(operator.outcomes()):
            predicates = list()
            mask = effect.mask
            factor_list = _extract_factors(mask, factors)
            if len(factor_list) == 1:
                # Independent. Go with it as-is.
                predicate = vocabulary.append(effect)
                predicates.append(predicate)
            else:
                show('{} factors:'.format(len(factor_list)), verbose)
                # Integrate all combinations of factors (massive explosion here!)
                for L in range(0, len(factor_list)):
                    for subset in itertools.combinations(factor_list, L):
                        new_symbol = effect
                        for factor in subset:
                            new_symbol = new_symbol.integrate_out(factor)
                        predicate = vocabulary.append(effect)
                        predicates.append(predicate)
            show('{} propositions generated'.format(len(predicates)), verbose)
            operator_predicates[(operator, i)] = predicates
    return operator_predicates


def _generate_start_distribution(transition_data: pd.DataFrame, verbose=False, **kwargs) -> KernelDensityEstimator:
    """
    Generate a distribution over the starting states
    :param transition_data: the transition data from which we can extract the initial states
    :param verbose: the verbosity level
    :return: a density estimation of the initial states
    """
    show("Fitting estimator to initial states", verbose)
    # group by episode and get the first state from each
    initial_states = pd2np(transition_data.groupby('episode').nth(0)['state'])
    full_mask = list(range(initial_states.shape[1]))  # all the state variables
    effect = KernelDensityEstimator(full_mask)
    effect.fit(initial_states, verbose=verbose, **kwargs)
    return effect


def _modifies(operators: List[LearnedOperator], n_variables: int) -> Dict[int, List[Tuple[int, int]]]:
    """
    Determine which options modify each state variable
    :param operators:
    :param n_variables:
    :return: For each state variable, a list of option-effect pairs that modify it
    """
    modifies = dict()
    for x in range(n_variables):
        new_mods = list()
        for i, operator in enumerate(operators):
            for v, (_, effect, _) in enumerate(operator.outcomes()):
                mask = effect.mask
                if x in mask:
                    new_mods.append((i, v))  # modifies[s] -> [(operator, effect number], ...]
        modifies[x] = new_mods

    return modifies


def _factorise(operators: List[LearnedOperator], n_variables: int, verbose=True) -> List[List[int]]:
    """
    Factorise the state space based on what variables are changed by the options. For more, see the JAIR 2018 paper
    :param operators: the learned operators
    :param n_variables: the number of state-space variables
    :param verbose: the verbosity level
    :return: factors: for each factor, the list of state variables
    """
    modifies = _modifies(operators, n_variables)  # check which variables are modified by each operator
    factors = list()
    options = list()

    for i in range(n_variables):
        found = False
        for x in range(len(factors)):
            f = factors[x]
            if options[x] == modifies[i]:
                f.append(i)
                found = True

        if not found:
            factors.append([i])
            options.append(modifies[i])

    show("Factors\tVariables\t\tOptions" + '\n'.join(
        ["F_{}\t\t{}\t{}".format(i, factors[i], options[i]) for i in range(len(factors))]), verbose)

    return factors


def _extract_factors(mask, factors):
    """
    Extract the factors referred to by the mask
    :param mask: the mask
    :param factors: the factors
    """
    ret = []
    m_set = set(mask)
    for f in factors:
        f_set = set(f)
        if not f_set.isdisjoint(m_set):
            part = f_set.intersection(m_set)
            ret.append(part)
            m_set = m_set - f_set

            if len(m_set) == 0:
                return ret
    warn("No overlapping factors in mask?!")
    return ret


def _mask_to_factors(mask, factors):
    """
    Convert a mask to factors
    :param mask: the mask
    :param factors: the factors
    :return: the index of factors referred to by the mask
    """
    f_list = []
    for index, f in enumerate(factors):
        found = False
        for m in mask:
            if m in f:
                found = True
                break
        if found:
            f_list.append(index)
    return f_list
