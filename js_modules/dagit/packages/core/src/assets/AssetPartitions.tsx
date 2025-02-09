import {Box, Colors, Icon, Spinner, Subheading} from '@dagster-io/ui';
import * as React from 'react';

import {LiveDataForNode} from '../asset-graph/Utils';
import {PartitionRangeWizard} from '../partitions/PartitionRangeWizard';
import {PartitionStateCheckboxes} from '../partitions/PartitionStateCheckboxes';
import {PartitionState} from '../partitions/PartitionStatus';
import {RepositorySelector} from '../types/globalTypes';

import {AssetPartitionDetailEmpty, AssetPartitionDetailLoader} from './AssetPartitionDetail';
import {AssetPartitionList} from './AssetPartitionList';
import {AssetViewParams} from './AssetView';
import {CurrentRunsBanner} from './CurrentRunsBanner';
import {FailedRunsSinceMaterializationBanner} from './FailedRunsSinceMaterializationBanner';
import {explodePartitionKeysInRanges, isTimeseriesPartition} from './MultipartitioningSupport';
import {AssetKey} from './types';
import {usePartitionDimensionRanges} from './usePartitionDimensionRanges';
import {PartitionHealthDimensionRange, usePartitionHealthData} from './usePartitionHealthData';

interface Props {
  assetKey: AssetKey;
  assetPartitionNames?: string[];
  liveData?: LiveDataForNode;
  params: AssetViewParams;
  paramsTimeWindowOnly: boolean;
  setParams: (params: AssetViewParams) => void;

  // This timestamp is a "hint", when it changes this component will refetch
  // to retrieve new data. Just don't want to poll the entire table query.
  assetLastMaterializedAt: string | undefined;

  repository?: RepositorySelector;
  opName?: string | null;
}

export const AssetPartitions: React.FC<Props> = ({
  assetKey,
  assetPartitionNames,
  params,
  setParams,
  liveData,
}) => {
  const [assetHealth] = usePartitionHealthData([assetKey]);
  const [ranges, setRanges] = usePartitionDimensionRanges(assetHealth, assetPartitionNames, 'view');

  const [stateFilters, setStateFilters] = React.useState<PartitionState[]>([
    PartitionState.MISSING,
    PartitionState.SUCCESS_MISSING,
    PartitionState.SUCCESS,
  ]);

  const timeRangeIdx = ranges.findIndex((r) => isTimeseriesPartition(r.dimension.partitionKeys[0]));
  const timeRange = timeRangeIdx !== -1 ? ranges[timeRangeIdx] : null;

  const allInRanges = React.useMemo(() => {
    return assetHealth ? explodePartitionKeysInRanges(ranges, assetHealth.stateForKey) : [];
  }, [ranges, assetHealth]);

  const allSelected = React.useMemo(
    () => allInRanges.filter((p) => stateFilters.includes(p.state)),
    [allInRanges, stateFilters],
  );

  const focusedDimensionKeys = params.partition
    ? ranges.length > 1
      ? params.partition.split('|').filter(Boolean)
      : [params.partition] // "|" character is allowed in 1D partition keys for historical reasons
    : [];

  const dimensionKeysOrdered = (range: PartitionHealthDimensionRange) => {
    return isTimeseriesPartition(range.selected[0])
      ? [...range.selected].reverse()
      : range.selected;
  };
  const dimensionRowsForRange = (range: PartitionHealthDimensionRange, idx: number) => {
    if (timeRange && timeRange.selected.length === 0) {
      return [];
    }
    return dimensionKeysOrdered(range)
      .map((dimensionKey) => {
        const state =
          focusedDimensionKeys.length >= idx
            ? assetHealth.stateForPartialKey([...focusedDimensionKeys.slice(0, idx), dimensionKey])
            : assetHealth.stateForSingleDimension(idx, dimensionKey, ranges[idx - 1]?.selected);

        return {dimensionKey, state};
      })
      .filter((row) => stateFilters.includes(row.state));
  };

  return (
    <>
      <FailedRunsSinceMaterializationBanner
        liveData={liveData}
        border={{side: 'bottom', width: 1, color: Colors.KeylineGray}}
      />

      <CurrentRunsBanner
        liveData={liveData}
        border={{side: 'bottom', width: 1, color: Colors.KeylineGray}}
      />
      {timeRange && (
        <Box
          padding={{vertical: 16, horizontal: 24}}
          border={{side: 'bottom', width: 1, color: Colors.KeylineGray}}
        >
          <PartitionRangeWizard
            partitionKeys={timeRange.dimension.partitionKeys}
            partitionStateForKey={(dimensionKey) =>
              assetHealth.stateForSingleDimension(timeRangeIdx, dimensionKey)
            }
            selected={timeRange.selected}
            setSelected={(selected) =>
              setRanges(ranges.map((r) => (r === timeRange ? {...r, selected} : r)))
            }
          />
        </Box>
      )}

      <Box
        padding={{vertical: 16, horizontal: 24}}
        flex={{direction: 'row', justifyContent: 'space-between'}}
        border={{side: 'bottom', width: 1, color: Colors.KeylineGray}}
      >
        <div>{allSelected.length.toLocaleString()} Partitions Selected</div>
        <PartitionStateCheckboxes
          partitionKeysForCounts={allInRanges}
          allowed={[PartitionState.MISSING, PartitionState.SUCCESS_MISSING, PartitionState.SUCCESS]}
          value={stateFilters}
          onChange={setStateFilters}
        />
      </Box>
      <Box style={{flex: 1, minHeight: 0, outline: 'none'}} flex={{direction: 'row'}} tabIndex={-1}>
        {ranges.map((range, idx) => (
          <Box
            key={range.dimension.name}
            style={{display: 'flex', flex: 1, paddingRight: 1}}
            flex={{direction: 'column'}}
            border={{side: 'right', color: Colors.KeylineGray, width: 1}}
            background={Colors.Gray50}
          >
            {range.dimension.name !== 'default' && (
              <Box
                padding={{horizontal: 24, vertical: 8}}
                flex={{gap: 8, alignItems: 'center'}}
                background={Colors.White}
                border={{side: 'bottom', width: 1, color: Colors.KeylineGray}}
              >
                <Icon name="partition" />
                <Subheading>{range.dimension.name}</Subheading>
              </Box>
            )}

            {!assetHealth ? (
              <Box flex={{alignItems: 'center', justifyContent: 'center'}} style={{flex: 1}}>
                <Spinner purpose="section" />
              </Box>
            ) : (
              <AssetPartitionList
                partitions={dimensionRowsForRange(range, idx)}
                focusedDimensionKey={focusedDimensionKeys[idx]}
                setFocusedDimensionKey={(dimensionKey) => {
                  const nextFocusedDimensionKeys: string[] = [];
                  for (let ii = 0; ii < idx; ii++) {
                    nextFocusedDimensionKeys.push(
                      focusedDimensionKeys[ii] || dimensionKeysOrdered(ranges[ii])[0],
                    );
                  }
                  if (dimensionKey) {
                    nextFocusedDimensionKeys.push(dimensionKey);
                  }
                  setParams({
                    ...params,
                    partition: nextFocusedDimensionKeys.join('|'),
                  });
                }}
              />
            )}
          </Box>
        ))}

        <Box style={{flex: 3}} flex={{direction: 'column'}}>
          {params.partition && focusedDimensionKeys.length === ranges.length ? (
            <AssetPartitionDetailLoader assetKey={assetKey} partitionKey={params.partition} />
          ) : (
            <AssetPartitionDetailEmpty />
          )}
        </Box>
      </Box>
    </>
  );
};
