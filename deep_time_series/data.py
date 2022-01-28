import numpy as np
import pandas as pd
from torch.utils.data import Dataset, DataLoader


class RangeChunkSpec:
    def __init__(self, tag, names, range_, dtype):
        self.tag = tag
        self.names = names
        self.range_ = range_
        self.dtype = dtype


class ChunkSpec:
    def __init__(self, tag, names, dtype, shift=0):
        self.tag = tag
        self.names = names
        self.shift = shift
        self.dtype = dtype

    def to_range_chunk_spec(self, encoding_length, decoding_length):
        range_ = (
            encoding_length - self.shift,
            encoding_length + decoding_length - self.shift
        )

        return RangeChunkSpec(
            tag=self.tag, names=self.names,
            range_=range_, dtype=self.dtype,
        )


class EncodingChunkSpec(ChunkSpec):
    def __init__(self, tag, names, dtype, shift=0):
        super().__init__(
            tag=f'encoding.{tag}',
            names=names,
            shift=shift,
            dtype=dtype,
        )


class DecodingChunkSpec(ChunkSpec):
    def __init__(self, tag, names, dtype, shift=0):
        super().__init__(
            tag=f'decoding.{tag}',
            names=names,
            shift=shift,
            dtype=dtype,
        )


class LabelChunkSpec(ChunkSpec):
    def __init__(self, tag, names, dtype, shift=0):
        super().__init__(
            tag=f'label.{tag}',
            names=names,
            shift=shift,
            dtype=dtype,
        )


class ChunkExtractor:
    def __init__(self, df, range_chunk_specs):
        # Check tag duplication.
        tags = [spec.tag for spec in range_chunk_specs]
        assert len(tags) == len(set(tags))

        self.range_chunk_specs = range_chunk_specs
        self.chunk_length = max(spec.range_[1] for spec in range_chunk_specs)

        self._preprocess(df)

    def _preprocess(self, df):
        self.data = {}
        for spec in self.range_chunk_specs:
            self.data[spec.tag] = df[spec.names].astype(spec.dtype).values

    def extract(self, start_time_index):
        chunk_dict = {}
        for spec in self.range_chunk_specs:
            array = self.data[spec.tag][
                start_time_index : start_time_index+self.chunk_length
            ]

            chunk_dict[spec.tag] = array[slice(*spec.range_)]

        return chunk_dict


class FeatureTransformers:
    def __init__(self, transformer_dict):
        self.transformer_dict = transformer_dict

    def _apply_to_single_feature(self, series, func):
        values = series.values.reshape(-1, 1)
        return_value = func(values)
        if isinstance(return_value, np.ndarray):
            return return_value.reshape(-1)
        else:
            return return_value

    def _append_index_and_id(self, data, df):
        for name in ['time_index', 'time_series_id']:
            if name in df.columns:
                data[name] = df[name]

    def _get_valid_names(self, names):
        valid_name_set = set(self.transformer_dict.keys()) & set(names)
        return [
            name for name in names if name in valid_name_set
        ]

    def fit(self, df):
        for name in self._get_valid_names(df.columns):
            transformer = self.transformer_dict[name]

            self._apply_to_single_feature(
                df[name], transformer.fit
            )

    def transform(self, df):
        data = {}
        for name in self._get_valid_names(df.columns):
            transformer = self.transformer_dict[name]

            data[name] = self._apply_to_single_feature(
                df[name], transformer.transform
            )

        self._append_index_and_id(data, df)
        return pd.DataFrame(data=data, index=df.index)

    def fit_transform(self, df):
        data = {}
        for name in self._get_valid_names(df.columns):
            transformer = self.transformer_dict[name]

            data[name] = self._apply_to_single_feature(
                df[name], transformer.fit_transform
            )

        self._append_index_and_id(data, df)
        return pd.DataFrame(data=data, index=df.index)

    def inverse_transform(self, df):
        data = {}
        for name in self._get_valid_names(df.columns):
            transformer = self.transformer_dict[name]

            data[name] = self._apply_to_single_feature(
                df[name], transformer.inverse_transform
            )

        self._append_index_and_id(data, df)
        return pd.DataFrame(data=data, index=df.index)


class TimeSeriesDataset(Dataset):
    def __init__(self,
        df,
        encoding_length,
        decoding_length,
        chunk_specs,
        feature_transformers,
        fit_feature_transformers=True,
    ):
        self.df = df.copy()
        self.encoding_length = encoding_length,
        self.decoding_length = decoding_length,
        # Make chunk_specs from encoding, decoding and label specs.
        self.chunk_specs = [
            spec.to_range_chunk_spec(encoding_length, decoding_length)
            for spec in chunk_specs
        ]

        self.feature_transformers = feature_transformers
        self.fit_feature_transformers = fit_feature_transformers

        self._preprocess()

    def _preprocess(self):
        self.df.sort_values(by='time_index', inplace=True)
        if self.fit_feature_transformers:
            self.feature_transformers.fit(self.df)

        self.scaled_df = self.feature_transformers.transform(self.df)

        splitted_dfs = [
            df for _, df in self.scaled_df.groupby('time_series_id')
        ]

        self.chunk_extractors = [
            ChunkExtractor(df, self.chunk_specs) for df in splitted_dfs
        ]

        self.lengths = [
            len(df) - self.chunk_extractors[0].chunk_length
            for df in splitted_dfs
        ]

    def __len__(self):
        return sum(self.lengths)

    def __getitem__(self, i):
        cumsum = np.cumsum([0] + self.lengths)
        df_index = np.argmax(cumsum - i > 0) - 1

        chunk_extractor = self.chunk_extractors[df_index]
        start_time_index = i - cumsum[df_index]

        chunk_dict = chunk_extractor.extract(start_time_index)

        return chunk_dict

    def convert_item_to_df(self, item):
        tag_to_names_dict = {
            spec.tag: spec.names
            for spec in self.chunk_extractors[0].chunk_specs
        }
        output = {}
        for tag, values in item.items():
            data = {}
            names = tag_to_names_dict[tag]
            for name, series in zip(names, values.T):
                data[name] = series
            df = pd.DataFrame(data=data)
            df = self.feature_transformers.inverse_transform(df)
            output[tag] = df

        return output
