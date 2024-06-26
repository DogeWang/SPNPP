import copy
import logging
from time import perf_counter

import numpy as np
from spn.algorithms.Inference import likelihood
from spn.structure.Base import Product

from rspn.code_generation.convert_conditions import convert_range
from rspn.structure.base import Sum
from rspn.structure.leaves import identity_expectation_ids, identity_likelihood_range_ids, categorical_likelihood_range_ids, pre_identity_likelihood_range_ids, list_identity_likelihood_range_ids, pre_ec_identity_likelihood_range_ids
from roaringbitmap import RoaringBitmap
from rspn.structure.leaves import Categorical, IdentityNumericLeaf
from utils.toolkit import leaf_calculation
logger = logging.getLogger(__name__)


def expectation(spn, feature_scope, inverted_features, ranges, node_expectation=None, node_likelihoods=None,
                use_generated_code=False, spn_id=None, meta_types=None, gen_code_stats=None):
    """Compute the Expectation:
        E[1_{conditions} * X_feature_scope]
        First factor is one if condition is fulfilled. For the second factor the variables in feature scope are
        multiplied. If inverted_features[i] is True, variable is taken to denominator.
        The conditional expectation would be E[1_{conditions} * X_feature_scope]/P(conditions)
    """

    # evidence_scope = set([i for i, r in enumerate(ranges) if not np.isnan(r)])
    evidence_scope = set([i for i, r in enumerate(ranges[0]) if r is not None])
    evidence = ranges
    assert not (len(evidence_scope) > 0 and evidence is None)

    relevant_scope = set()
    relevant_scope.update(evidence_scope)
    relevant_scope.update(feature_scope)
    if len(relevant_scope) == 0:
        return np.ones((ranges.shape[0], 1))

    if ranges.shape[0] == 1:

        applicable = True
        if use_generated_code:
            boolean_relevant_scope = [i in relevant_scope for i in range(len(meta_types))]
            boolean_feature_scope = [i in feature_scope for i in range(len(meta_types))]
            applicable, parameters = convert_range(boolean_relevant_scope, boolean_feature_scope, meta_types, ranges[0],
                                                   inverted_features)

        # generated C++ code
        if use_generated_code and applicable:
            time_start = perf_counter()
            import optimized_inference

            spn_func = getattr(optimized_inference, f'spn{spn_id}')
            result = np.array([[spn_func(*parameters)]])

            time_end = perf_counter()

            if gen_code_stats is not None:
                gen_code_stats.calls += 1
                gen_code_stats.total_time += (time_end - time_start)

            return result

        else:
            temp = np.array(
                [[expectation_recursive(spn, feature_scope, inverted_features, relevant_scope, evidence,
                                        node_expectation, node_likelihoods)[0]]])
            return temp

    return expectation_recursive_batch(spn, feature_scope, inverted_features, relevant_scope, evidence,
                                       node_expectation, node_likelihoods)


def expectation_recursive_batch(node, feature_scope, inverted_features, relevant_scope, evidence, node_expectation,
                                node_likelihoods):
    if isinstance(node, Product):
        llchildren = np.concatenate(
            [expectation_recursive_batch(child, feature_scope, inverted_features, relevant_scope, evidence,
                                         node_expectation, node_likelihoods)
             for child in node.children if
             len(relevant_scope.intersection(child.scope)) > 0], axis=1)
        return np.nanprod(llchildren, axis=1).reshape(-1, 1)

    elif isinstance(node, Sum):
        if len(relevant_scope.intersection(node.scope)) == 0:
            return np.full((evidence.shape[0], 1), np.nan)
        llchildren = np.concatenate(
            [expectation_recursive_batch(child, feature_scope, inverted_features, relevant_scope, evidence,
                                         node_expectation, node_likelihoods)
             for child in node.children], axis=1)

        relevant_children_idx = np.where(np.isnan(llchildren[0]) == False)[0]
        if len(relevant_children_idx) == 0:
            return np.array([np.nan])

        weights_normalizer = sum(node.weights[j] for j in relevant_children_idx)
        b = np.array(node.weights)[relevant_children_idx] / weights_normalizer

        return np.dot(llchildren[:, relevant_children_idx], b).reshape(-1, 1)

    else:
        if node.scope[0] in feature_scope:
            t_node = type(node)
            if t_node in node_expectation:
                exps = np.zeros((evidence.shape[0], 1))

                feature_idx = feature_scope.index(node.scope[0])
                inverted = inverted_features[feature_idx]

                exps[:] = node_expectation[t_node](node, evidence, inverted=inverted)
                return exps
            else:
                raise Exception('Node type unknown: ' + str(t_node))
        return likelihood(node, evidence, node_likelihood=node_likelihoods)


def nanproduct(product, factor):
    if np.isnan(product):
        if not np.isnan(factor):
            return factor
        else:
            return np.nan
    else:
        if np.isnan(factor):
            return product
        else:
            return product * factor


def idsproduct(c_ids, ids_list):
    if not isinstance(c_ids, RoaringBitmap):
        if isinstance(ids_list, RoaringBitmap):
            return ids_list
        else:
            return None
    else:
        if not isinstance(ids_list, RoaringBitmap):
            return c_ids
        else:
            return ids_list & c_ids

# estimate results by using bitmaps
def expectation_recursive(node, feature_scope, inverted_features, relevant_scope, evidence, node_expectation,
                          node_likelihoods, c_ids=None, spn=False):
    if isinstance(node, Product):
        temp_range = set(list(evidence[:, node.scope][0]))
        if None in temp_range:
            temp_range.remove(None)
        if len(temp_range) <= 1 and not feature_scope:
            spn = True
        product = np.nan
        in_product, out_product = np.nan, np.nan
        product_c = 1
        c_ids = [None]
        is_leaf, is_exp, is_out, is_large = False, False, False, False
        exp_child_list, o_child_list = [], []
        intersection_p = 1
        for child in node.children:
            if len(relevant_scope.intersection(child.scope)) > 0 and child.scope[0] in feature_scope \
                    and type(child) in node_expectation and not isinstance(child, Product) and not isinstance(child, Sum):
                exp_child_list.append(child)
            elif len(relevant_scope.intersection(child.scope)) > 0:
                o_child_list.append(child)
        for child in o_child_list:
            factor, ids_list, temp_intersection_p = expectation_recursive(child, feature_scope, inverted_features, relevant_scope,
                                                               evidence,
                                                               node_expectation, node_likelihoods, spn=spn)
            if is_large:
                product = nanproduct(product, factor)
                continue
            if not isinstance(ids_list, RoaringBitmap):
                product = nanproduct(product, factor)
                if isinstance(temp_intersection_p, float):
                    is_large = True
                    if isinstance(c_ids, RoaringBitmap):
                        product = nanproduct(product, len(c_ids) / node.cardinality + out_product)
                #         print(product)
            elif not isinstance(c_ids, RoaringBitmap):
                in_product = 0.0
                is_leaf = True
                c_ids = ids_list
            elif len(c_ids) != node.cardinality and len(ids_list) == node.cardinality:
                in_product = len(c_ids) / node.cardinality
                is_leaf = True
                c_ids = c_ids
            elif len(c_ids) == node.cardinality and len(ids_list) != node.cardinality:
                in_product = len(c_ids) / node.cardinality
                is_leaf = True
                c_ids = ids_list
            else:
                in_product = len(c_ids) / node.cardinality
                is_leaf = True
                c_ids = idsproduct(c_ids, ids_list)
            if isinstance(temp_intersection_p, float):
                if np.isnan(out_product):
                    out_product = temp_intersection_p
                else:
                    is_out = True
                    out_product = nanproduct(in_product, temp_intersection_p) + nanproduct(len(ids_list) / node.cardinality + temp_intersection_p, out_product)
        if isinstance(c_ids, RoaringBitmap) and c_ids:
            product_c = len(c_ids) / node.cardinality
        i_num = 0
        for child in exp_child_list:
            if isinstance(c_ids, RoaringBitmap) and c_ids:
                factor, ids_list, factor_out = expectation_recursive(child, feature_scope, inverted_features, relevant_scope,
                                                               evidence,
                                                               node_expectation, node_likelihoods, c_ids=c_ids, spn=spn)
                if out_product > 0:
                    factor = (factor * len(c_ids) + factor_out * out_product * node.cardinality) / (len(c_ids) + out_product * node.cardinality)
                is_leaf = True
                is_exp = True
                c_ids = idsproduct(c_ids, ids_list)
                if len(ids_list) == 0:
                    product = 0
                    continue
                elif np.isnan(product):
                    i_num += 1
                    product = factor
                else:
                    i_num += 1
                    product *= factor

                if product_c < 1:
                    product_c = len(ids_list) / node.cardinality
            elif isinstance(c_ids, RoaringBitmap):
                if np.isnan(out_product):
                    return 0, [None], False
                elif out_product <= 0:
                    return 0, [None], False
                else:
                    factor, _, _ = expectation_recursive(child, feature_scope, inverted_features,
                                                                         relevant_scope,
                                                                         evidence,
                                                                         node_expectation, node_likelihoods)
                    product = nanproduct(product, factor)
            else:
                factor, _, _ = expectation_recursive(child, feature_scope, inverted_features,
                                                                     relevant_scope,
                                                                     evidence,
                                                                     node_expectation, node_likelihoods,spn=True)
                product = nanproduct(product, factor)

        if isinstance(c_ids, RoaringBitmap) and not np.isnan(product) and is_exp:
            product *= len(c_ids) / node.cardinality
        elif isinstance(c_ids, RoaringBitmap) and is_leaf and not is_large:
            temp_product = len(c_ids) / node.cardinality
            if not np.isnan(out_product) and is_out and temp_product:
                temp_product += out_product
            if np.isnan(product):
                product = temp_product
            else:
                product *= temp_product
        return product, [None], False

    elif isinstance(node, Sum):
        if len(relevant_scope.intersection(node.scope)) == 0:
            return np.nan, [None], False

        llchildren = [expectation_recursive(child, feature_scope, inverted_features, relevant_scope, evidence,
                                            node_expectation, node_likelihoods)[0]
                      for child in node.children]

        relevant_children_idx = np.where(np.isnan(llchildren) == False)[0]

        if len(relevant_children_idx) == 0:
            return np.nan, [None], False

        weights_normalizer = sum(node.weights[j] for j in relevant_children_idx)
        weighted_sum = sum(node.weights[j] * llchildren[j] for j in relevant_children_idx)
        return weighted_sum / weights_normalizer, [None], False

    else:
        if spn:
            if node.scope[0] in feature_scope:
                t_node = type(node)
                if t_node in node_expectation:
                    feature_idx = feature_scope.index(node.scope[0])
                    inverted = inverted_features[feature_idx]
                    return node_expectation[t_node](node, evidence, inverted=inverted).item(), [None], False
                else:
                    raise Exception('Node type unknown: ' + str(t_node))
            temp = node_likelihoods[type(node)](node, evidence)
            return temp.item(), [None], False
        else:
            if node.scope[0] in feature_scope:
                t_node = type(node)
                if t_node in node_expectation:
                    feature_idx = feature_scope.index(node.scope[0])
                    inverted = inverted_features[feature_idx]
                    temp = identity_expectation_ids(node, evidence, inverted=inverted, c_ids=c_ids)
                    return temp[0].item(), temp[1], temp[2].item()
                else:
                    raise Exception('Node type unknown: ' + str(t_node))
            if isinstance(node, IdentityNumericLeaf):
                if node.pre_ids_bitmap:
                    temp = pre_ec_identity_likelihood_range_ids(node, evidence)
                else:
                    temp = identity_likelihood_range_ids(node, evidence)
            elif isinstance(node, Categorical):
                temp = categorical_likelihood_range_ids(node, evidence)
            return temp[0].item(), temp[1], temp[2]

