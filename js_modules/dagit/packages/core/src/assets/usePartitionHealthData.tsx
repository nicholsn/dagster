import {ApolloClient, gql, useApolloClient} from '@apollo/client';
import React from 'react';

import {PartitionState} from '../partitions/PartitionStatus';

import {mergedStates} from './MultipartitioningSupport';
import {AssetKey} from './types';
import {PartitionHealthQuery, PartitionHealthQueryVariables} from './types/PartitionHealthQuery';

/**
 * usePartitionHealthData retrieves partitionKeysByDimension + partitionMaterializationCounts and
 * reshapes the data for rapid retrieval from the UI. The hook exposes a series of getter methods
 * for each asset's data, hiding the underlying data structures from the rest of the app.
 *
 * The hope is that if we want to add support for 3- and 4- dimension partitioned assets, all
 * of the changes will be in this file. The rest of the app already supports N dimensions.
 */

export interface PartitionHealthData {
  assetKey: AssetKey;
  dimensions: PartitionHealthDimension[];
  stateForKey: (dimensionKeys: string[]) => PartitionState;
  stateForPartialKey: (dimensionKeys: string[]) => PartitionState;
  stateForSingleDimension: (
    dimensionIdx: number,
    dimensionKey: string,
    withinParentDimensions?: string[],
  ) => PartitionState;
}

export interface PartitionHealthDimension {
  name: string;
  partitionKeys: string[];
}

export type PartitionHealthDimensionRange = {
  dimension: PartitionHealthDimension;
  selected: string[];
};

async function loadPartitionHealthData(client: ApolloClient<any>, loadKey: AssetKey) {
  const {data} = await client.query<PartitionHealthQuery, PartitionHealthQueryVariables>({
    query: PARTITION_HEALTH_QUERY,
    fetchPolicy: 'network-only',
    variables: {
      assetKey: {path: loadKey.path},
    },
  });

  const dimensions =
    data.assetNodeOrError.__typename === 'AssetNode'
      ? data.assetNodeOrError.partitionKeysByDimension
      : [];

  const counts = (data.assetNodeOrError.__typename === 'AssetNode' &&
    data.assetNodeOrError.partitionMaterializationCounts) || {
    __typename: 'MaterializationCountSingleDimension',
    materializationCounts: [],
  };

  const stateByKey = Object.fromEntries(
    counts.__typename === 'MaterializationCountSingleDimension'
      ? counts.materializationCounts.map((count, idx) => [
          dimensions[0].partitionKeys[idx],
          count > 0 ? PartitionState.SUCCESS : PartitionState.MISSING,
        ])
      : counts.materializationCountsGrouped.map((dim0, idx0) => [
          dimensions[0].partitionKeys[idx0],
          Object.fromEntries(
            dim0.map((count, idx1) => [
              dimensions[1].partitionKeys[idx1],
              count > 0 ? PartitionState.SUCCESS : PartitionState.MISSING,
            ]),
          ),
        ]),
  );

  const stateForKey = (dimensionKeys: string[]): PartitionState =>
    dimensionKeys.reduce((counts, dimensionKey) => counts[dimensionKey], stateByKey);

  const stateForSingleDimension = (
    dimensionIdx: number,
    dimensionKey: string,
    withinParentDimensions?: string[],
  ) => {
    if (dimensionIdx === 0) {
      return stateForPartialKey([dimensionKey]);
    } else if (dimensionIdx === 1) {
      return mergedStates(
        Object.entries<{[subdimensionKey: string]: PartitionState}>(stateByKey)
          .filter(([key]) => !withinParentDimensions || withinParentDimensions.includes(key))
          .map(([_, val]) => val[dimensionKey]),
      );
    } else {
      throw new Error('stateForSingleDimension asked for third dimension');
    }
  };

  const stateForPartialKey = (dimensionKeys: string[]) => {
    return dimensionKeys.length === dimensions.length
      ? stateForKey(dimensionKeys)
      : mergedStates(Object.values(stateByKey[dimensionKeys[0]]));
  };

  const result: PartitionHealthData = {
    assetKey: loadKey,
    stateForKey,
    stateForPartialKey,
    stateForSingleDimension,
    dimensions: dimensions.map((d) => ({
      name: d.name,
      partitionKeys: d.partitionKeys,
    })),
  };

  return result;
}

export function usePartitionHealthData(assetKeys: AssetKey[]) {
  const [result, setResult] = React.useState<PartitionHealthData[]>([]);
  const client = useApolloClient();

  const assetKeyJSONs = assetKeys.map((k) => JSON.stringify(k));
  const assetKeyJSON = JSON.stringify(assetKeyJSONs);
  const missingKeyJSON = assetKeyJSONs.find(
    (k) => !result.some((r) => JSON.stringify(r.assetKey) === k),
  );

  React.useMemo(() => {
    if (!missingKeyJSON) {
      return;
    }
    const loadKey: AssetKey = JSON.parse(missingKeyJSON);
    const run = async () => {
      const loaded = await loadPartitionHealthData(client, loadKey);
      setResult((result) => [...result, loaded]);
    };
    run();
  }, [client, missingKeyJSON]);

  return React.useMemo(() => {
    const assetKeyJSONs = JSON.parse(assetKeyJSON);
    return result.filter((r) => assetKeyJSONs.includes(JSON.stringify(r.assetKey)));
  }, [assetKeyJSON, result]);
}

const PARTITION_HEALTH_QUERY = gql`
  query PartitionHealthQuery($assetKey: AssetKeyInput!) {
    assetNodeOrError(assetKey: $assetKey) {
      ... on AssetNode {
        id
        partitionKeysByDimension {
          name
          partitionKeys
        }
        partitionMaterializationCounts {
          ... on MaterializationCountGroupedByDimension {
            materializationCountsGrouped
          }
          ... on MaterializationCountSingleDimension {
            materializationCounts
          }
        }
      }
    }
  }
`;
