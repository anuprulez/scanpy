"""Rank genes according to differential expression.
"""
from math import sqrt, floor
from typing import Iterable, Union, Optional

import numpy as np
import pandas as pd
from anndata import AnnData
from scipy.sparse import issparse, vstack

from .. import _utils
from .. import logging as logg
from ..preprocessing._simple import _get_mean_var
from .._compat import Literal


_Method = Literal['logreg', 't-test', 'wilcoxon', 't-test_overestim_var']
_CorrMethod = Literal['benjamini-hochberg', 'bonferroni']


def _select_top_n(scores, n_top, n_from):
    reference_indices = np.arange(n_from, dtype=int)
    partition = np.argpartition(scores, -n_top)[-n_top:]
    partial_indices = np.argsort(scores[partition])[::-1]
    global_indices = reference_indices[partition][partial_indices]

    return global_indices


def _t_test(
    X,
    var_names,
    method,
    corr_method,
    expm1_func,
    rankby_abs,
    n_genes_user,
    groups_order,
    groups_masks,
    ireference,
):
    from scipy import stats
    from statsmodels.stats.multitest import multipletests

    n_genes = X.shape[1]
    n_groups = groups_masks.shape[0]
    d = dict(scores={}, names={}, logfoldchanges={}, pvals={}, pvals_adj={})

    # loop over all masks and compute means, variances and sample numbers
    means = np.zeros((n_groups, n_genes))
    vars = np.zeros((n_groups, n_genes))
    ns = np.count_nonzero(groups_masks, axis=1)

    for imask, mask in enumerate(groups_masks):
        means[imask], vars[imask] = _get_mean_var(X[mask])

    # test each either against the union of all other groups or against a
    # specific group
    for igroup, group_name in enumerate(groups_order):
        if ireference is None:
            mask_rest = ~groups_masks[igroup]
        else:
            if igroup == ireference:
                continue
            else:
                mask_rest = groups_masks[ireference]
        mean_group, var_group = means[igroup], vars[igroup]
        mean_rest, var_rest = _get_mean_var(X[mask_rest])

        # number of observations in group
        ns_group = ns[igroup]
        if method == 't-test':
            ns_rest = np.count_nonzero(mask_rest)
        elif method == 't-test_overestim_var':
            # hack for overestimating the variance for small groups
            ns_rest = ns_group
        else:
            raise ValueError('Method does not exist.')

        # TODO: Come up with better solution. Mask unexpressed genes?
        # See https://github.com/scipy/scipy/issues/10269
        with np.errstate(invalid="ignore"):
            scores, pvals = stats.ttest_ind_from_stats(
                mean1=mean_group,
                std1=np.sqrt(var_group),
                nobs1=ns_group,
                mean2=mean_rest,
                std2=np.sqrt(var_rest),
                nobs2=ns_rest,
                equal_var=False,  # Welch's
            )

        # Fold change
        foldchanges = (expm1_func(mean_group) + 1e-9) / (
            expm1_func(mean_rest) + 1e-9
        )  # add small value to remove 0's

        # I think it's only nan when means are the same and vars are 0
        scores[np.isnan(scores)] = 0
        # This also has to happen for Benjamini Hochberg
        pvals[np.isnan(pvals)] = 1

        if corr_method == 'benjamini-hochberg':
            _, pvals_adj, _, _ = multipletests(pvals, alpha=0.05, method='fdr_bh')
        elif corr_method == 'bonferroni':
            pvals_adj = np.minimum(pvals * n_genes, 1.0)

        scores_sort = np.abs(scores) if rankby_abs else scores
        global_indices = _select_top_n(scores_sort, n_genes_user, n_genes)

        d['scores'][group_name] = scores[global_indices]
        d['logfoldchanges'][group_name] = np.log2(foldchanges[global_indices])
        d['names'][group_name] = var_names[global_indices]
        d['pvals'][group_name] = pvals[global_indices]
        d['pvals_adj'][group_name] = pvals_adj[global_indices]

    return d


def _ranks_generator(X, mask=None, mask_rest=None):
    CONST_MAX_SIZE = 10000000

    n_genes = X.shape[1]

    if issparse(X):
        merge = lambda tpl: vstack(tpl).todense()
        adapt = lambda X: X.todense()
    else:
        merge = np.vstack
        adapt = lambda X: X

    masked = mask is not None and mask_rest is not None

    if masked:
        n_cells = np.count_nonzero(mask) + np.count_nonzero(mask_rest)
        get_chunk = lambda X, left, right: merge(
            (X[mask, left:right], X[mask_rest, left:right])
        )
    else:
        n_cells = X.shape[0]
        get_chunk = lambda X, left, right: adapt(X[:, left:right])

    # Now calculate gene expression ranking in chunkes:
    chunk = []
    # Calculate chunk frames
    n_genes_max_chunk = floor(CONST_MAX_SIZE / n_cells)
    if n_genes_max_chunk < n_genes:
        chunk_index = n_genes_max_chunk
        while chunk_index < n_genes:
            chunk.append(chunk_index)
            chunk_index = chunk_index + n_genes_max_chunk
        chunk.append(n_genes)
    else:
        chunk.append(n_genes)

    left = 0
    # Calculate rank sums for each chunk for the current mask
    for chunk_index, right in enumerate(chunk):

        df = pd.DataFrame(data=get_chunk(X, left, right))
        ranks = df.rank()
        yield ranks, left, right

        left = right


def _wilcoxon(
    X,
    var_names,
    corr_method,
    expm1_func,
    rankby_abs,
    n_genes_user,
    groups_order,
    groups_masks,
    ireference,
):
    from scipy import stats
    from statsmodels.stats.multitest import multipletests

    n_genes = X.shape[1]
    n_groups = groups_masks.shape[0]
    d = dict(scores={}, names={}, logfoldchanges={}, pvals={}, pvals_adj={})

    means = np.zeros((n_groups, n_genes))
    ns = np.count_nonzero(groups_masks, axis=1)
    # initialize space for z-scores
    scores = np.zeros(n_genes)
    # First loop: Loop over all genes
    if ireference is not None:
        for imask, mask in enumerate(groups_masks):
            # for fold-change only
            means[imask], _ = _get_mean_var(X[mask])

            if imask == ireference:
                continue

            else:
                mask_rest = groups_masks[ireference]
            ns_rest = np.count_nonzero(mask_rest)
            # for fold-change only
            mean_rest, _ = _get_mean_var(X[mask_rest])

            if ns_rest <= 25 or ns[imask] <= 25:
                logg.hint(
                    'Few observations in a group for '
                    'normal approximation (<=25). Lower test accuracy.'
                )
            n_active = ns[imask]
            m_active = ns_rest

            # Calculate rank sums for each chunk for the current mask
            for ranks, left, right in _ranks_generator(X, mask, mask_rest):
                scores[left:right] = np.sum(ranks.loc[0:n_active, :])

            scores = (scores - (n_active * ((n_active + m_active + 1) / 2))) / sqrt(
                (n_active * m_active / 12 * (n_active + m_active + 1))
            )
            scores[np.isnan(scores)] = 0
            pvals = 2 * stats.distributions.norm.sf(np.abs(scores))

            if corr_method == 'benjamini-hochberg':
                # set Nan values to 1 to properly convert using Benhjamini Hochberg
                pvals[np.isnan(pvals)] = 1
                _, pvals_adj, _, _ = multipletests(pvals, alpha=0.05, method='fdr_bh')
            elif corr_method == 'bonferroni':
                pvals_adj = np.minimum(pvals * n_genes, 1.0)

            # Fold change
            foldchanges = (expm1_func(means[imask]) + 1e-9) / (
                expm1_func(mean_rest) + 1e-9
            )  # add small value to remove 0's
            scores_sort = np.abs(scores) if rankby_abs else scores
            global_indices = _select_top_n(scores_sort, n_genes_user, n_genes)

            d['scores'][groups_order[imask]] = scores[global_indices]
            d['logfoldchanges'][groups_order[imask]] = np.log2(
                foldchanges[global_indices]
            )
            d['names'][groups_order[imask]] = var_names[global_indices]
            d['pvals'][groups_order[imask]] = pvals[global_indices]
            d['pvals_adj'][groups_order[imask]] = pvals_adj[global_indices]

    # If no reference group exists,
    # ranking needs only to be done once (full mask)
    else:
        scores = np.zeros((n_groups, n_genes))
        n_cells = X.shape[0]

        for ranks, left, right in _ranks_generator(X):
            # sum up adjusted_ranks to calculate W_m,n
            for imask, mask in enumerate(groups_masks):
                scores[imask, left:right] = np.sum(ranks.loc[mask, :])

        for imask, mask in enumerate(groups_masks):
            mask_rest = ~groups_masks[imask]
            # for fold-change
            means[imask], _ = _get_mean_var(X[mask])
            mean_rest, _ = _get_mean_var(X[mask_rest])

            scores[imask, :] = (
                scores[imask, :] - (ns[imask] * (n_cells + 1) / 2)
            ) / sqrt((ns[imask] * (n_cells - ns[imask]) / 12 * (n_cells + 1)))
            scores[np.isnan(scores)] = 0
            pvals = 2 * stats.distributions.norm.sf(np.abs(scores[imask, :]))

            if corr_method == 'benjamini-hochberg':
                # set NaN values to 1 to convert using Benjamini Hochberg
                pvals[np.isnan(pvals)] = 1
                _, pvals_adj, _, _ = multipletests(pvals, alpha=0.05, method='fdr_bh')
            elif corr_method == 'bonferroni':
                pvals_adj = np.minimum(pvals * n_genes, 1.0)

            # Fold change
            foldchanges = (expm1_func(means[imask]) + 1e-9) / (
                expm1_func(mean_rest) + 1e-9
            )  # add small value to remove 0's

            scores_sort = np.abs(scores) if rankby_abs else scores
            global_indices = _select_top_n(scores_sort[imask], n_genes_user, n_genes)

            d['scores'][groups_order[imask]] = scores[imask, global_indices]
            d['logfoldchanges'][groups_order[imask]] = np.log2(
                foldchanges[global_indices]
            )
            d['names'][groups_order[imask]] = var_names[global_indices]
            d['pvals'][groups_order[imask]] = pvals[global_indices]
            d['pvals_adj'][groups_order[imask]] = pvals_adj[global_indices]

    return d


def _logreg(X, var_names, n_genes_user, grouping, groups_order, **kwds):
    # if reference is not set, then the groups listed will be compared to the rest
    # if reference is set, then the groups listed will be compared only to the other groups listed
    from sklearn.linear_model import LogisticRegression

    if len(groups_order) == 1:
        raise Exception('Cannot perform logistic regression on a single cluster.')

    n_genes = X.shape[1]
    d = dict(scores={}, names={})

    clf = LogisticRegression(**kwds)
    clf.fit(X, grouping.cat.codes)
    scores_all = clf.coef_
    for igroup, group_name in enumerate(groups_order):
        if len(groups_order) <= 2:  # binary logistic regression
            scores = scores_all[0]
        else:
            scores = scores_all[igroup]

        global_indices = _select_top_n(scores, n_genes_user, n_genes)

        d['scores'][group_name] = scores[global_indices]
        d['names'][group_name] = var_names[global_indices]
        if len(groups_order) <= 2:
            break

    return d


def rank_genes_groups(
    adata: AnnData,
    groupby: str,
    use_raw: bool = True,
    groups: Union[Literal['all'], Iterable[str]] = 'all',
    reference: str = 'rest',
    n_genes: int = 100,
    rankby_abs: bool = False,
    key_added: Optional[str] = None,
    copy: bool = False,
    method: _Method = 't-test',
    corr_method: _CorrMethod = 'benjamini-hochberg',
    layer: Optional[str] = None,
    **kwds,
) -> Optional[AnnData]:
    """\
    Rank genes for characterizing groups.

    Parameters
    ----------
    adata
        Annotated data matrix.
    groupby
        The key of the observations grouping to consider.
    use_raw
        Use `raw` attribute of `adata` if present.
    layer
        Key from `adata.layers` whose value will be used to perform tests on.
    groups
        Subset of groups, e.g. [`'g1'`, `'g2'`, `'g3'`], to which comparison
        shall be restricted, or `'all'` (default), for all groups.
    reference
        If `'rest'`, compare each group to the union of the rest of the group.
        If a group identifier, compare with respect to this group.
    n_genes
        The number of genes that appear in the returned tables.
    method
        The default 't-test_overestim_var' overestimates variance of each group,
        `'t-test'` uses t-test, `'wilcoxon'` uses Wilcoxon rank-sum,
        `'logreg'` uses logistic regression. See [Ntranos18]_,
        `here <https://github.com/theislab/scanpy/issues/95>`__ and `here
        <http://www.nxn.se/valent/2018/3/5/actionable-scrna-seq-clusters>`__,
        for why this is meaningful.
    corr_method
        p-value correction method.
        Used only for `'t-test'`, `'t-test_overestim_var'`, and `'wilcoxon'`.
    rankby_abs
        Rank genes by the absolute value of the score, not by the
        score. The returned scores are never the absolute values.
    key_added
        The key in `adata.uns` information is saved to.
    **kwds
        Are passed to test methods. Currently this affects only parameters that
        are passed to :class:`sklearn.linear_model.LogisticRegression`.
        For instance, you can pass `penalty='l1'` to try to come up with a
        minimal set of genes that are good predictors (sparse solution meaning
        few non-zero fitted coefficients).

    Returns
    -------
    **names** : structured `np.ndarray` (`.uns['rank_genes_groups']`)
        Structured array to be indexed by group id storing the gene
        names. Ordered according to scores.
    **scores** : structured `np.ndarray` (`.uns['rank_genes_groups']`)
        Structured array to be indexed by group id storing the z-score
        underlying the computation of a p-value for each gene for each
        group. Ordered according to scores.
    **logfoldchanges** : structured `np.ndarray` (`.uns['rank_genes_groups']`)
        Structured array to be indexed by group id storing the log2
        fold change for each gene for each group. Ordered according to
        scores. Only provided if method is 't-test' like.
        Note: this is an approximation calculated from mean-log values.
    **pvals** : structured `np.ndarray` (`.uns['rank_genes_groups']`)
        p-values.
    **pvals_adj** : structured `np.ndarray` (`.uns['rank_genes_groups']`)
        Corrected p-values.

    Notes
    -----
    There are slight inconsistencies depending on whether sparse
    or dense data are passed. See `here <https://github.com/theislab/scanpy/blob/master/scanpy/tests/test_rank_genes_groups.py>`__.

    Examples
    --------
    >>> import scanpy as sc
    >>> adata = sc.datasets.pbmc68k_reduced()
    >>> sc.tl.rank_genes_groups(adata, 'bulk_labels', method='wilcoxon')

    # to visualize the results
    >>> sc.pl.rank_genes_groups(adata)
    """
    logg.warning(
        "Default of the method has been changed to 't-test' from 't-test_overestim_var'"
    )

    if 'only_positive' in kwds:
        rankby_abs = not kwds.pop('only_positive')  # backwards compat

    start = logg.info('ranking genes')
    avail_methods = {'t-test', 't-test_overestim_var', 'wilcoxon', 'logreg'}
    if method not in avail_methods:
        raise ValueError(f'Method must be one of {avail_methods}.')

    avail_corr = {'benjamini-hochberg', 'bonferroni'}
    if corr_method not in avail_corr:
        raise ValueError(f'Correction method must be one of {avail_corr}.')

    adata = adata.copy() if copy else adata
    _utils.sanitize_anndata(adata)
    # for clarity, rename variable
    if groups == 'all':
        groups_order = 'all'
    elif isinstance(groups, (str, int)):
        raise ValueError('Specify a sequence of groups')
    else:
        groups_order = list(groups)
        if isinstance(groups_order[0], int):
            groups_order = [str(n) for n in groups_order]
        if reference != 'rest' and reference not in set(groups_order):
            groups_order += [reference]
    if reference != 'rest' and reference not in set(adata.obs[groupby].cat.categories):
        cats = adata.obs[groupby].cat.categories.tolist()
        raise ValueError(
            f'reference = {reference} needs to be one of groupby = {cats}.'
        )

    groups_order, groups_masks = _utils.select_groups(adata, groups_order, groupby)

    if key_added is None:
        key_added = 'rank_genes_groups'
    adata.uns[key_added] = {}
    adata.uns[key_added]['params'] = dict(
        groupby=groupby,
        reference=reference,
        method=method,
        use_raw=use_raw,
        layer=layer,
        corr_method=corr_method,
    )

    # adata_comp mocks an AnnData object if use_raw is True
    # otherwise it's just the AnnData object
    adata_comp = adata
    if layer is not None:
        if use_raw:
            raise ValueError("Cannot specify `layer` and have `use_raw=True`.")
        X = adata_comp.layers[layer]
    else:
        if use_raw and adata.raw is not None:
            adata_comp = adata.raw
        X = adata_comp.X

    # for clarity, rename variable
    n_genes_user = n_genes
    # make sure indices are not OoB in case there are less genes than n_genes
    if n_genes_user > X.shape[1]:
        n_genes_user = X.shape[1]

    ns = np.count_nonzero(groups_masks, axis=1)
    logg.debug(f'consider {groupby!r} groups:')
    logg.debug(f'with sizes: {ns}')
    del ns

    ireference = None
    if reference != 'rest':
        ireference = np.where(groups_order == reference)[0][0]

    if 'log1p' in adata.uns_keys() and adata.uns['log1p']['base'] is not None:
        expm1_func = lambda x: np.expm1(x * np.log(adata.uns['log1p']['base']))
    else:
        expm1_func = np.expm1

    if method in {'t-test', 't-test_overestim_var'}:
        d = _t_test(
            X,
            adata_comp.var_names,
            method,
            corr_method,
            expm1_func,
            rankby_abs,
            n_genes_user,
            groups_order,
            groups_masks,
            ireference,
        )

    elif method == 'wilcoxon':
        d = _wilcoxon(
            X,
            adata_comp.var_names,
            corr_method,
            expm1_func,
            rankby_abs,
            n_genes_user,
            groups_order,
            groups_masks,
            ireference,
        )

    elif method == 'logreg':
        grouping_mask = adata.obs[groupby].isin(groups_order)
        grouping = adata.obs.loc[grouping_mask, groupby]
        # Indexing with a series causes issues, possibly segfault
        X = X[grouping_mask.values, :]

        d = _logreg(
            X, adata_comp.var_names, n_genes_user, grouping, groups_order, **kwds
        )

    group_names = [str(name) for name in d['scores']]
    n_groups = len(group_names)
    dtypes = {
        'names': ['U50'] * n_groups,
        'scores': ['float32'] * n_groups,
        'logfoldchanges': ['float32'] * n_groups,
        'pvals': ['float64'] * n_groups,
        'pvals_adj': ['float64'] * n_groups,
    }
    for k in d:
        adata.uns[key_added][k] = np.rec.fromarrays(
            d[k].values(), names=group_names, formats=dtypes[k]
        )

    logg.info(
        '    finished',
        time=start,
        deep=(
            f'added to `.uns[{key_added!r}]`\n'
            "    'names', sorted np.recarray to be indexed by group ids\n"
            "    'scores', sorted np.recarray to be indexed by group ids\n"
            + (
                "    'logfoldchanges', sorted np.recarray to be indexed by group ids\n"
                "    'pvals', sorted np.recarray to be indexed by group ids\n"
                "    'pvals_adj', sorted np.recarray to be indexed by group ids"
                if method in {'t-test', 't-test_overestim_var', 'wilcoxon'}
                else ''
            )
        ),
    )
    return adata if copy else None


def filter_rank_genes_groups(
    adata: AnnData,
    key=None,
    groupby=None,
    use_raw=True,
    log=True,
    key_added='rank_genes_groups_filtered',
    min_in_group_fraction=0.25,
    min_fold_change=2,
    max_out_group_fraction=0.5,
) -> None:
    """\
    Filters out genes based on fold change and fraction of genes expressing the
    gene within and outside the `groupby` categories.

    See :func:`~scanpy.tl.rank_genes_groups`.

    Results are stored in `adata.uns[key_added]`
    (default: 'rank_genes_groups_filtered').

    To preserve the original structure of adata.uns['rank_genes_groups'],
    filtered genes are set to `NaN`.

    Parameters
    ----------
    adata
    key
    groupby
    use_raw
    log
        If true, it means that the values to work with are in log scale
    key_added
    min_in_group_fraction
    min_fold_change
    max_out_group_fraction

    Returns
    -------
    Same output as :func:`scanpy.tl.rank_genes_groups` but with filtered genes names set to
    `nan`

    Examples
    --------
    >>> import scanpy as sc
    >>> adata = sc.datasets.pbmc68k_reduced()
    >>> sc.tl.rank_genes_groups(adata, 'bulk_labels', method='wilcoxon')
    >>> sc.tl.filter_rank_genes_groups(adata, min_fold_change=3)
    >>> # visualize results
    >>> sc.pl.rank_genes_groups(adata, key='rank_genes_groups_filtered')
    >>> # visualize results using dotplot
    >>> sc.pl.rank_genes_groups_dotplot(adata, key='rank_genes_groups_filtered')
    """
    if key is None:
        key = 'rank_genes_groups'

    if groupby is None:
        groupby = str(adata.uns[key]['params']['groupby'])

    # convert structured numpy array into DataFrame
    gene_names = pd.DataFrame(adata.uns[key]['names'])

    fraction_in_cluster_matrix = pd.DataFrame(
        np.zeros(gene_names.shape), columns=gene_names.columns, index=gene_names.index,
    )
    fold_change_matrix = pd.DataFrame(
        np.zeros(gene_names.shape), columns=gene_names.columns, index=gene_names.index,
    )
    fraction_out_cluster_matrix = pd.DataFrame(
        np.zeros(gene_names.shape), columns=gene_names.columns, index=gene_names.index,
    )
    logg.info(
        f"Filtering genes using: "
        f"min_in_group_fraction: {min_in_group_fraction} "
        f"min_fold_change: {min_fold_change}, "
        f"max_out_group_fraction: {max_out_group_fraction}"
    )
    from ..plotting._anndata import _prepare_dataframe

    for cluster in gene_names.columns:
        # iterate per column
        var_names = gene_names[cluster].values

        # add column to adata as __is_in_cluster__. This facilitates to measure
        # fold change of each gene with respect to all other clusters
        adata.obs['__is_in_cluster__'] = pd.Categorical(adata.obs[groupby] == cluster)

        # obs_tidy has rows=groupby, columns=var_names
        categories, obs_tidy = _prepare_dataframe(
            adata, var_names, groupby='__is_in_cluster__', use_raw=use_raw,
        )

        # for if category defined by groupby (if any) compute for each var_name
        # 1. the mean value over the category
        # 2. the fraction of cells in the category having a value > 0

        # 1. compute mean value
        mean_obs = obs_tidy.groupby(level=0).mean()

        # 2. compute fraction of cells having value >0
        # transform obs_tidy into boolean matrix
        obs_bool = obs_tidy.astype(bool)

        # compute the sum per group which in the boolean matrix this is the number
        # of values >0, and divide the result by the total number of values in the group
        # (given by `count()`)
        fraction_obs = (
            obs_bool.groupby(level=0).sum() / obs_bool.groupby(level=0).count()
        )

        # Because the dataframe groupby is based on the '__is_in_cluster__' column,
        # in this context, [True] means __is_in_cluster__.
        # Also, in this context, fraction_obs.loc[True].values is the row of values
        # that is assigned *as column* to fraction_in_cluster_matrix to follow the
        # structure of the gene_names dataFrame
        fraction_in_cluster_matrix.loc[:, cluster] = fraction_obs.loc[True].values
        fraction_out_cluster_matrix.loc[:, cluster] = fraction_obs.loc[False].values

        # compute fold change.
        if log:
            fold_change_matrix.loc[:, cluster] = (
                np.exp(mean_obs.loc[True]) / np.exp(mean_obs.loc[False])
            ).values
        else:
            fold_change_matrix.loc[:, cluster] = (
                mean_obs.loc[True] / mean_obs.loc[False]
            ).values

    # remove temporary columns
    adata.obs.drop(columns='__is_in_cluster__')
    # filter original_matrix
    gene_names = gene_names[
        (fraction_in_cluster_matrix > min_in_group_fraction)
        & (fraction_out_cluster_matrix < max_out_group_fraction)
        & (fold_change_matrix > min_fold_change)
    ]
    # create new structured array using 'key_added'.
    adata.uns[key_added] = adata.uns[key].copy()
    adata.uns[key_added]['names'] = gene_names.to_records(index=False)
