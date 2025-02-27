"""Base Class for tabular models."""

import functools
import logging
import math
import os
import pickle
import uuid
from collections import defaultdict
from copy import deepcopy

import copulas
import numpy as np
import pandas as pd
import tqdm

from sdv.errors import ConstraintsNotMetError
from sdv.metadata import Table
from sdv.tabular.utils import check_num_rows, handle_sampling_error
from sdv.utils import get_package_versions, throw_version_mismatch_warning

LOGGER = logging.getLogger(__name__)
COND_IDX = str(uuid.uuid4())
FIXED_RNG_SEED = 73251
TMP_FILE_NAME = '.sample.csv.temp'
DISABLE_TMP_FILE = 'disable'


class NonParametricError(Exception):
    """Exception to indicate that a model is not parametric."""


class BaseTabularModel:
    """Base class for all the tabular models.

    The ``BaseTabularModel`` class defines the common API that all the
    TabularModels need to implement, as well as common functionality.

    Args:
        field_names (list[str]):
            List of names of the fields that need to be modeled
            and included in the generated output data. Any additional
            fields found in the data will be ignored and will not be
            included in the generated output.
            If ``None``, all the fields found in the data are used.
        field_types (dict[str, dict]):
            Dictinary specifying the data types and subtypes
            of the fields that will be modeled. Field types and subtypes
            combinations must be compatible with the SDV Metadata Schema.
        field_transformers (dict[str, str]):
            Dictinary specifying which transformers to use for each field.
            Available transformers are:

                * ``FloatFormatter``: Uses a ``FloatFormatter`` for numerical data.
                * ``FrequencyEncoder``: Uses a ``FrequencyEncoder`` without gaussian noise.
                * ``FrequencyEncoder_noised``: Uses a ``FrequencyEncoder`` adding gaussian noise.
                * ``OneHotEncoder``: Uses a ``OneHotEncoder``.
                * ``LabelEncoder``: Uses a ``LabelEncoder`` without gaussian nose.
                * ``LabelEncoder_noised``: Uses a ``LabelEncoder`` adding gaussian noise.
                * ``BinaryEncoder``: Uses a ``BinaryEncoder``.
                * ``UnixTimestampEncoder``: Uses a ``UnixTimestampEncoder``.

        anonymize_fields (dict[str, str]):
            Dict specifying which fields to anonymize and what faker
            category they belong to.
        primary_key (str):
            Name of the field which is the primary key of the table.
        constraints (list[Constraint, dict]):
            List of Constraint objects or dicts.
        table_metadata (dict or metadata.Table):
            Table metadata instance or dict representation.
            If given alongside any other metadata-related arguments, an
            exception will be raised.
            If not given at all, it will be built using the other
            arguments or learned from the data.
        learn_rounding_scheme (bool):
            Define rounding scheme for ``FloatFormatter``. If ``True``, the data returned by
            ``reverse_transform`` will be rounded to that place. Defaults to ``True``.
        enforce_min_max_values (bool):
            Specify whether or not to clip the data returned by ``reverse_transform`` of
            the numerical transformer, ``FloatFormatter``, to the min and max values seen
            during ``fit``. Defaults to ``True``.
    """

    _DTYPE_TRANSFORMERS = None

    _metadata = None

    def __init__(self, field_names=None, field_types=None, field_transformers=None,
                 anonymize_fields=None, primary_key=None, constraints=None, table_metadata=None,
                 learn_rounding_scheme=True, enforce_min_max_values=True):
        if table_metadata is None:
            self._metadata = Table(
                field_names=field_names,
                primary_key=primary_key,
                field_types=field_types,
                field_transformers=field_transformers,
                anonymize_fields=anonymize_fields,
                constraints=constraints,
                dtype_transformers=self._DTYPE_TRANSFORMERS,
                learn_rounding_scheme=learn_rounding_scheme,
                enforce_min_max_values=enforce_min_max_values
            )
            self._metadata_fitted = False
        else:
            table_metadata = deepcopy(table_metadata)
            for arg in (field_names, primary_key, field_types, anonymize_fields, constraints):
                if arg:
                    raise ValueError(
                        'If table_metadata is given {} must be None'.format(arg.__name__))

            if isinstance(table_metadata, dict):
                table_metadata = Table.from_dict(table_metadata)

            table_metadata._dtype_transformers.update(self._DTYPE_TRANSFORMERS)

            self._metadata = table_metadata
            self._metadata_fitted = table_metadata.fitted

    def fit(self, data):
        """Fit this model to the data.

        If the table metadata has not been given, learn it from the data.

        Args:
            data (pandas.DataFrame or str):
                Data to fit the model to. It can be passed as a
                ``pandas.DataFrame`` or as an ``str``.
                If an ``str`` is passed, it is assumed to be
                the path to a CSV file which can be loaded using
                ``pandas.read_csv``.
        """
        if isinstance(data, pd.DataFrame):
            data = data.reset_index(drop=True)

        LOGGER.debug('Fitting %s to table %s; shape: %s', self.__class__.__name__,
                     self._metadata.name, data.shape)
        if not self._metadata_fitted:
            self._metadata.fit(data)

        self._num_rows = len(data)

        LOGGER.debug('Transforming table %s; shape: %s', self._metadata.name, data.shape)
        transformed = self._metadata.transform(data)

        if self._metadata.get_dtypes(ids=False):
            LOGGER.debug(
                'Fitting %s model to table %s', self.__class__.__name__, self._metadata.name)
            self._fit(transformed)

    def get_metadata(self):
        """Get metadata about the table.

        This will return an ``sdv.metadata.Table`` object containing
        the information about the data that this model has learned.

        This Table metadata will contain some common information,
        such as field names and data types, as well as additional
        information that each Sub-class might add, such as the
        observed data field distributions and their parameters.

        Returns:
            sdv.metadata.Table:
                Table metadata.
        """
        return self._metadata

    @staticmethod
    def _filter_conditions(sampled, conditions, float_rtol):
        """Filter the sampled rows that match the conditions.

        If condition columns are float values, consider a match anything that
        is closer than the given ``float_rtol`` and then make the value exact.

        Args:
            sampled (pandas.DataFrame):
                The sampled rows, reverse transformed.
            conditions (dict):
                The dictionary of conditioning values.
            float_rtol (float):
                Maximum tolerance when considering a float match.

        Returns:
            pandas.DataFrame:
                Rows from the sampled data that match the conditions.
        """
        for column, value in conditions.items():
            column_values = sampled[column]
            if column_values.dtype.kind == 'f':
                distance = value * float_rtol
                sampled = sampled[np.abs(column_values - value) <= distance]
                sampled[column] = value
            else:
                sampled = sampled[column_values == value]

        return sampled

    def _sample_rows(self, num_rows, conditions=None, transformed_conditions=None,
                     float_rtol=0.1, previous_rows=None):
        """Sample rows with the given conditions.

        Input conditions is taken both in the raw input format, which will be used
        for filtering during the reject-sampling loop, and already transformed
        to the model format, which will be passed down to the model if it supports
        conditional sampling natively.

        If condition columns are float values, consider a match anything that
        is closer than the given ``float_rtol`` and then make the value exact.

        If the model does not have any data columns, the result of this call
        is a dataframe of the requested length with no columns in it.

        Args:
            num_rows (int):
                Number of rows to sample.
            conditions (dict):
                The dictionary of conditioning values in the original format.
            transformed_conditions (dict):
                The dictionary of conditioning values transformed to the model format.
            float_rtol (float):
                Maximum tolerance when considering a float match.
            previous_rows (pandas.DataFrame):
                Valid rows sampled in the previous iterations.

        Returns:
            tuple:
                * pandas.DataFrame:
                    Rows from the sampled data that match the conditions.
                * int:
                    Number of rows that are considered valid.
        """
        if self._metadata.get_dtypes(ids=False):
            if conditions is None:
                sampled = self._sample(num_rows)
            else:
                try:
                    sampled = self._sample(num_rows, transformed_conditions)
                except NotImplementedError:
                    sampled = self._sample(num_rows)

            sampled = self._metadata.reverse_transform(sampled)

            if previous_rows is not None:
                sampled = pd.concat([previous_rows, sampled], ignore_index=True)

            sampled = self._metadata.filter_valid(sampled)

            if conditions is not None:
                sampled = self._filter_conditions(sampled, conditions, float_rtol)

            num_valid = len(sampled)

            return sampled, num_valid

        else:
            sampled = pd.DataFrame(index=range(num_rows))
            sampled = self._metadata.reverse_transform(sampled)
            return sampled, num_rows

    def _sample_batch(self, batch_size=None, max_tries=100,
                      conditions=None, transformed_conditions=None, float_rtol=0.01,
                      progress_bar=None, output_file_path=None):
        """Sample a batch of rows with the given conditions.

        This will enter a reject-sampling loop in which rows will be sampled until
        all of them are valid and match the requested conditions. If `max_tries`
        is exceeded, it will return as many rows as it has sampled, which may be less
        than the target number of rows.

        Input conditions is taken both in the raw input format, which will be used
        for filtering during the reject-sampling loop, and already transformed
        to the model format, which will be passed down to the model if it supports
        conditional sampling natively.

        If condition columns are float values, consider a match anything that is
        relatively closer than the given ``float_rtol`` and then make the value exact.

        If the model does not have any data columns, the result of this call
        is a dataframe of the requested length with no columns in it.

        Args:
            batch_size (int):
                Number of rows to sample for this batch. If not given the model
                will generate as many rows as there were in the
                data passed to the ``fit`` method.
            max_tries (int):
                Number of times to retry sampling until the batch size is met.
                Defaults to 100.
            conditions (dict):
                The dictionary of conditioning values in the original input format.
            transformed_conditions (dict):
                The dictionary of conditioning values transformed to the model format.
            float_rtol (float):
                Maximum tolerance when considering a float match.
            progress_bar (tqdm.tqdm or None):
                The progress bar to update when sampling. If None, a new tqdm progress
                bar will be created.
            output_file_path (str or None):
                The file to periodically write sampled rows to. If None, does not write
                rows anywhere.

        Returns:
            pandas.DataFrame:
                Sampled data.
        """
        num_rows_to_sample = batch_size

        counter = 0
        num_valid = 0
        prev_num_valid = None
        remaining = batch_size
        sampled = pd.DataFrame()

        while num_valid < batch_size:
            if counter >= max_tries:
                break

            prev_num_valid = num_valid
            sampled, num_valid = self._sample_rows(
                num_rows_to_sample, conditions, transformed_conditions, float_rtol, sampled,
            )

            num_new_valid_rows = num_valid - prev_num_valid
            num_increase = min(num_new_valid_rows, remaining)
            if num_increase > 0:
                if output_file_path:
                    append_kwargs = {'mode': 'a', 'header': False} if os.path.getsize(
                        output_file_path) > 0 else {}
                    sampled.head(min(len(sampled), batch_size)).tail(num_increase).to_csv(
                        output_file_path,
                        index=False,
                        **append_kwargs,
                    )
                if progress_bar is not None:
                    progress_bar.update(num_increase)

            remaining = batch_size - num_valid
            valid_rate = max(num_new_valid_rows, 1) / max(num_rows_to_sample, 1)
            num_rows_to_sample = min(10 * batch_size, int(remaining / valid_rate))

            if remaining > 0:
                LOGGER.info(
                    f'{remaining} valid rows remaining. Resampling {num_rows_to_sample} rows')
            counter += 1

        return sampled.head(min(len(sampled), batch_size))

    def _make_condition_dfs(self, conditions):
        """Transform `conditions` into a list of dataframes.

        Args:
            conditions (list[sdv.sampling.Condition]):
                A list of `sdv.sampling.Condition`, where each `Condition` object
                represents a desired column value mapping and the number of rows
                to generate for that condition.

        Returns:
            list[pandas.DataFrame]:
                A list of `conditions` as dataframes.
        """
        condition_dataframes = defaultdict(list)
        for condition in conditions:
            column_values = condition.get_column_values()
            condition_dataframes[tuple(column_values.keys())].append(
                pd.DataFrame(column_values, index=range(condition.get_num_rows())))

        return [
            pd.concat(condition_list, ignore_index=True)
            for condition_list in condition_dataframes.values()
        ]

    def _sample_in_batches(self, num_rows, batch_size, max_tries_per_batch, conditions=None,
                           transformed_conditions=None, float_rtol=0.01, progress_bar=None,
                           output_file_path=None):
        sampled = []
        batch_size = batch_size if num_rows > batch_size else num_rows
        for step in range(math.ceil(num_rows / batch_size)):
            sampled_rows = self._sample_batch(
                batch_size=batch_size,
                max_tries=max_tries_per_batch,
                conditions=conditions,
                transformed_conditions=transformed_conditions,
                float_rtol=float_rtol,
                progress_bar=progress_bar,
                output_file_path=output_file_path,
            )
            sampled.append(sampled_rows)

        sampled = pd.concat(sampled, ignore_index=True) if len(sampled) > 0 else pd.DataFrame()
        return sampled.head(num_rows)

    def _conditionally_sample_rows(self, dataframe, condition, transformed_condition,
                                   max_tries_per_batch=None, batch_size=None, float_rtol=0.01,
                                   graceful_reject_sampling=True, progress_bar=None,
                                   output_file_path=None):
        batch_size = batch_size or len(dataframe)
        sampled_rows = self._sample_in_batches(
            num_rows=len(dataframe),
            batch_size=batch_size,
            max_tries_per_batch=max_tries_per_batch,
            conditions=condition,
            transformed_conditions=transformed_condition,
            float_rtol=float_rtol,
            progress_bar=progress_bar,
            output_file_path=output_file_path
        )

        if len(sampled_rows) > 0:
            sampled_rows[COND_IDX] = dataframe[COND_IDX].values[:len(sampled_rows)]

        else:
            # Didn't get any rows.
            if not graceful_reject_sampling:
                user_msg = ('Unable to sample any rows for the given conditions '
                            f'`{transformed_condition}`. ')
                if hasattr(self, '_model') and isinstance(
                        self._model, copulas.multivariate.GaussianMultivariate):
                    user_msg = user_msg + (
                        'This may be because the provided values are out-of-bounds in the '
                        'current model. \nPlease try again with a different set of values.'
                    )
                else:
                    user_msg = user_msg + (
                        f'Try increasing `max_tries_per_batch` (currently: {max_tries_per_batch}) '
                        f'or increasing `batch_size` (currently: {batch_size}). Note that '
                        'increasing these values will also increase the sampling time.'
                    )

                raise ValueError(user_msg)

        return sampled_rows

    def _validate_file_path(self, output_file_path):
        """Validate the user-passed output file arg, and create the file."""
        output_path = None
        if output_file_path == DISABLE_TMP_FILE:
            # Temporary way of disabling the output file feature, used by HMA1.
            return output_path

        elif output_file_path:
            output_path = os.path.abspath(output_file_path)
            if os.path.exists(output_path):
                raise AssertionError(f'{output_path} already exists.')

        else:
            if os.path.exists(TMP_FILE_NAME):
                os.remove(TMP_FILE_NAME)

            output_path = TMP_FILE_NAME

        # Create the file.
        with open(output_path, 'w+'):
            pass

        return output_path

    def _randomize_samples(self, randomize_samples):
        """Randomize the samples according to user input.

        If ``randomize_samples`` is false, fix the seed that the random number generator
        uses in the underlying models.

        Args:
            randomize_samples (bool):
                Whether or not to randomize the generated samples.
        """
        if self._model is None:
            return

        if randomize_samples:
            self._set_random_state(None)
        else:
            self._set_random_state(FIXED_RNG_SEED)

    def _sample_with_progress_bar(self, num_rows, randomize_samples=True, max_tries_per_batch=100,
                                  batch_size=None, output_file_path=None, conditions=None,
                                  show_progress_bar=True):
        if conditions is not None:
            raise TypeError('This method does not support the conditions parameter. '
                            'Please create `sdv.sampling.Condition` objects and pass them '
                            'into the `sample_conditions` method. '
                            'See User Guide or API for more details.')

        if num_rows is None:
            raise ValueError('You must specify the number of rows to sample (e.g. num_rows=100).')

        if num_rows == 0:
            return pd.DataFrame()

        self._randomize_samples(randomize_samples)

        output_file_path = self._validate_file_path(output_file_path)

        batch_size = min(batch_size, num_rows) if batch_size else num_rows

        try:
            with tqdm.tqdm(total=num_rows, disable=not show_progress_bar) as progress_bar:
                progress_bar.set_description('Sampling rows')
                sampled = self._sample_in_batches(
                    num_rows=num_rows,
                    batch_size=batch_size,
                    max_tries_per_batch=max_tries_per_batch,
                    progress_bar=progress_bar,
                    output_file_path=output_file_path
                )

        except (Exception, KeyboardInterrupt) as error:
            handle_sampling_error(output_file_path == TMP_FILE_NAME, output_file_path, error)

        else:
            if output_file_path == TMP_FILE_NAME and os.path.exists(output_file_path):
                os.remove(output_file_path)

        return sampled

    def sample(self, num_rows, randomize_samples=True, max_tries_per_batch=100, batch_size=None,
               output_file_path=None, conditions=None):
        """Sample rows from this table.

        Args:
            num_rows (int):
                Number of rows to sample. This parameter is required.
            randomize_samples (bool):
                Whether or not to use a fixed seed when sampling. Defaults
                to True.
            max_tries_per_batch (int):
                Number of times to retry sampling until the batch size is met. Defaults to 100.
            batch_size (int or None):
                The batch size to sample. Defaults to `num_rows`, if None.
            output_file_path (str or None):
                The file to periodically write sampled rows to. If None, does not
                write rows anywhere.
            conditions:
                Deprecated argument. Use the `sample_conditions` method with
                `sdv.sampling.Condition` objects instead.

        Returns:
            pandas.DataFrame:
                Sampled data.
        """
        has_constraints = bool(self.get_metadata()._constraints)
        has_batches = batch_size is not None and batch_size != num_rows
        show_progress_bar = has_constraints or has_batches

        return self._sample_with_progress_bar(
            num_rows,
            randomize_samples,
            max_tries_per_batch,
            batch_size,
            output_file_path,
            conditions,
            show_progress_bar=show_progress_bar
        )

    def _validate_conditions(self, conditions):
        """Validate the user-passed conditions."""
        for column in conditions.columns:
            if column not in self._metadata.get_fields():
                raise ValueError(f'Unexpected column name `{column}`. '
                                 f'Use a column name that was present in the original data.')

    def _sample_with_conditions(self, conditions, max_tries_per_batch, batch_size,
                                progress_bar=None, output_file_path=None):
        """Sample rows with conditions.

        Args:
            conditions (pandas.DataFrame):
                A DataFrame representing the conditions to be sampled.
            max_tries_per_batch (int):
                Number of times to retry sampling until the batch size is met. Defaults to 100.
            batch_size (int):
                The batch size to use for each sampling call.
            progress_bar (tqdm.tqdm or None):
                The progress bar to update.
            output_file_path (str or None):
                The file to periodically write sampled rows to. Defaults to
                a temporary file, if None.

        Returns:
            pandas.DataFrame:
                Sampled data.

        Raises:
            ConstraintsNotMetError:
                If the conditions are not valid for the given constraints.
            ValueError:
                If any of the following happens:
                    * any of the conditions' columns are not valid.
                    * no rows could be generated.
        """
        condition_columns = list(conditions.columns)
        conditions.index.name = COND_IDX
        conditions.reset_index(inplace=True)
        grouped_conditions = conditions.groupby(condition_columns)

        # sample
        all_sampled_rows = list()

        for group, dataframe in grouped_conditions:
            if not isinstance(group, tuple):
                group = [group]

            condition = dict(zip(condition_columns, group))
            condition_df = dataframe.iloc[0].to_frame().T
            try:
                transformed_condition = self._metadata.transform(condition_df, is_condition=True)
            except ConstraintsNotMetError as cnme:
                cnme.message = 'Provided conditions are not valid for the given constraints'
                raise
            transformed_conditions = pd.concat(
                [transformed_condition] * len(dataframe),
                ignore_index=True
            )
            transformed_columns = list(transformed_conditions.columns)
            transformed_conditions[COND_IDX] = dataframe[COND_IDX]

            if len(transformed_columns) == 0:
                sampled_rows = self._conditionally_sample_rows(
                    dataframe=dataframe,
                    condition=condition,
                    transformed_condition=None,
                    max_tries_per_batch=max_tries_per_batch,
                    batch_size=batch_size,
                    progress_bar=progress_bar,
                    output_file_path=output_file_path,
                )
                all_sampled_rows.append(sampled_rows)
            else:
                transformed_groups = transformed_conditions.groupby(transformed_columns)
                for transformed_group, transformed_dataframe in transformed_groups:
                    if not isinstance(transformed_group, tuple):
                        transformed_group = [transformed_group]

                    transformed_condition = dict(zip(transformed_columns, transformed_group))
                    sampled_rows = self._conditionally_sample_rows(
                        dataframe=transformed_dataframe,
                        condition=condition,
                        transformed_condition=transformed_condition,
                        max_tries_per_batch=max_tries_per_batch,
                        batch_size=batch_size,
                        progress_bar=progress_bar,
                        output_file_path=output_file_path,
                    )
                    all_sampled_rows.append(sampled_rows)

        all_sampled_rows = pd.concat(all_sampled_rows)
        if len(all_sampled_rows) == 0:
            return all_sampled_rows

        all_sampled_rows = all_sampled_rows.set_index(COND_IDX)
        all_sampled_rows.index.name = conditions.index.name
        all_sampled_rows = all_sampled_rows.sort_index()
        all_sampled_rows = self._metadata.make_ids_unique(all_sampled_rows)

        return all_sampled_rows

    def _sample_conditions(self, conditions, max_tries_per_batch, batch_size, randomize_samples,
                           output_file_path):
        """Sample rows from this table with the given conditions."""
        output_file_path = self._validate_file_path(output_file_path)

        num_rows = functools.reduce(
            lambda num_rows, condition: condition.get_num_rows() + num_rows, conditions, 0)

        conditions = self._make_condition_dfs(conditions)
        for condition_dataframe in conditions:
            self._validate_conditions(condition_dataframe)

        self._randomize_samples(randomize_samples)

        sampled = pd.DataFrame()
        try:
            with tqdm.tqdm(total=num_rows) as progress_bar:
                progress_bar.set_description('Sampling conditions')
                for condition_dataframe in conditions:
                    sampled_for_condition = self._sample_with_conditions(
                        condition_dataframe,
                        max_tries_per_batch,
                        batch_size,
                        progress_bar,
                        output_file_path,
                    )
                    sampled = pd.concat([sampled, sampled_for_condition], ignore_index=True)

            is_reject_sampling = (hasattr(self, '_model') and not isinstance(
                self._model, copulas.multivariate.GaussianMultivariate))
            check_num_rows(
                num_rows=len(sampled),
                expected_num_rows=num_rows,
                is_reject_sampling=is_reject_sampling,
                max_tries_per_batch=max_tries_per_batch
            )

        except (Exception, KeyboardInterrupt) as error:
            handle_sampling_error(output_file_path == TMP_FILE_NAME, output_file_path, error)

        else:
            if output_file_path == TMP_FILE_NAME and os.path.exists(output_file_path):
                os.remove(output_file_path)

        return sampled

    def sample_conditions(self, conditions, max_tries_per_batch=100, batch_size=None,
                          randomize_samples=True, output_file_path=None):
        """Sample rows from this table with the given conditions.

        Args:
            conditions (list[sdv.sampling.Condition]):
                A list of sdv.sampling.Condition objects, which specify the column
                values in a condition, along with the number of rows for that
                condition.
            max_tries_per_batch (int):
                Number of times to retry sampling until the batch size is met. Defaults to 100.
            batch_size (int):
                The batch size to use per sampling call.
            randomize_samples (bool):
                Whether or not to use a fixed seed when sampling. Defaults
                to True.
            output_file_path (str or None):
                The file to periodically write sampled rows to. Defaults to
                a temporary file, if None.

        Returns:
            pandas.DataFrame:
                Sampled data.

        Raises:
            ConstraintsNotMetError:
                If the conditions are not valid for the given constraints.
            ValueError:
                If any of the following happens:
                    * any of the conditions' columns are not valid.
                    * no rows could be generated.
        """
        return self._sample_conditions(
            conditions, max_tries_per_batch, batch_size, randomize_samples, output_file_path)

    def _sample_remaining_columns(self, known_columns, max_tries_per_batch, batch_size,
                                  randomize_samples, output_file_path):
        """Sample the remaining columns of a given DataFrame."""
        output_file_path = self._validate_file_path(output_file_path)

        self._randomize_samples(randomize_samples)

        known_columns = known_columns.copy()
        self._validate_conditions(known_columns)
        sampled = pd.DataFrame()
        try:
            with tqdm.tqdm(total=len(known_columns)) as progress_bar:
                progress_bar.set_description('Sampling remaining columns')
                sampled = self._sample_with_conditions(
                    known_columns, max_tries_per_batch, batch_size, progress_bar, output_file_path)

            check_num_rows(
                num_rows=len(sampled),
                expected_num_rows=len(known_columns),
                is_reject_sampling=(hasattr(self, '_model') and isinstance(
                    self._model, copulas.multivariate.GaussianMultivariate)),
                max_tries_per_batch=max_tries_per_batch
            )

        except (Exception, KeyboardInterrupt) as error:
            handle_sampling_error(output_file_path == TMP_FILE_NAME, output_file_path, error)

        else:
            if output_file_path == TMP_FILE_NAME and os.path.exists(output_file_path):
                os.remove(output_file_path)

        return sampled

    def sample_remaining_columns(self, known_columns, max_tries_per_batch=100, batch_size=None,
                                 randomize_samples=True, output_file_path=None):
        """Sample rows from this table.

        Args:
            known_columns (pandas.DataFrame):
                A pandas.DataFrame with the columns that are already known. The output
                is a DataFrame such that each row in the output is sampled
                conditionally on the corresponding row in the input.
            max_tries_per_batch (int):
                Number of times to retry sampling until the batch size is met. Defaults to 100.
            batch_size (int):
                The batch size to use per sampling call.
            randomize_samples (bool):
                Whether or not to use a fixed seed when sampling. Defaults
                to True.
            output_file_path (str or None):
                The file to periodically write sampled rows to. Defaults to
                a temporary file, if None.

        Returns:
            pandas.DataFrame:
                Sampled data.

        Raises:
            ConstraintsNotMetError:
                If the conditions are not valid for the given constraints.
            ValueError:
                If any of the following happens:
                    * any of the conditions' columns are not valid.
                    * no rows could be generated.
        """
        return self._sample_remaining_columns(
            known_columns, max_tries_per_batch, batch_size, randomize_samples, output_file_path)

    def _get_parameters(self):
        raise NonParametricError()

    def get_parameters(self):
        """Get the parameters learned from the data.

        The result is a flat dict (single level) which contains
        all the necessary parameters to be able to reproduce
        this model.

        Subclasses which are not parametric, such as DeepLearning
        based models, raise a NonParametricError indicating that
        this method is not supported for their implementation.

        Returns:
            parameters (dict):
                flat dict (single level) which contains all the
                necessary parameters to be able to reproduce
                this model.

        Raises:
            NonParametricError:
                If the model is not parametric or cannot be described
                using a simple dictionary.
        """
        if self._metadata.get_dtypes(ids=False):
            parameters = self._get_parameters()
        else:
            parameters = {}

        parameters['num_rows'] = self._num_rows
        return parameters

    def _set_parameters(self, parameters):
        raise NonParametricError()

    def set_parameters(self, parameters):
        """Regenerate a previously learned model from its parameters.

        Subclasses which are not parametric, such as DeepLearning
        based models, raise a NonParametricError indicating that
        this method is not supported for their implementation.

        Args:
            dict:
                Model parameters.

        Raises:
            NonParametricError:
                If the model is not parametric or cannot be described
                using a simple dictionary.
        """
        num_rows = parameters.pop('num_rows')
        self._num_rows = 0 if pd.isnull(num_rows) else max(0, int(round(num_rows)))

        if self._metadata.get_dtypes(ids=False):
            self._set_parameters(parameters)

    def save(self, path):
        """Save this model instance to the given path using pickle.

        Args:
            path (str):
                Path where the SDV instance will be serialized.
        """
        self._package_versions = get_package_versions(getattr(self, '_model', None))

        with open(path, 'wb') as output:
            pickle.dump(self, output)

    @classmethod
    def load(cls, path):
        """Load a TabularModel instance from a given path.

        Args:
            path (str):
                Path from which to load the instance.

        Returns:
            TabularModel:
                The loaded tabular model.
        """
        with open(path, 'rb') as f:
            model = pickle.load(f)
            throw_version_mismatch_warning(getattr(model, '_package_versions', None))

            return model
