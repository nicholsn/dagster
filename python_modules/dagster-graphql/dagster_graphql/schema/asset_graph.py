from typing import TYPE_CHECKING, Dict, List, Optional, Sequence, Union, cast

import graphene
from dagster_graphql.implementation.events import iterate_metadata_entries
from dagster_graphql.schema.config_types import GrapheneConfigTypeField
from dagster_graphql.schema.metadata import GrapheneMetadataEntry
from dagster_graphql.schema.partition_sets import (
    GrapheneDimensionPartitionKeys,
    GraphenePartitionDefinition,
)
from dagster_graphql.schema.solids import (
    GrapheneCompositeSolidDefinition,
    GrapheneResourceRequirement,
    GrapheneSolidDefinition,
)

from dagster import AssetKey
from dagster import _check as check
from dagster._core.definitions.asset_graph import AssetGraph
from dagster._core.host_representation import ExternalRepository, RepositoryLocation
from dagster._core.host_representation.external import ExternalPipeline
from dagster._core.host_representation.external_data import (
    ExternalAssetNode,
    ExternalMultiPartitionsDefinitionData,
    ExternalStaticPartitionsDefinitionData,
    ExternalTimeWindowPartitionsDefinitionData,
)
from dagster._core.snap.solid import CompositeSolidDefSnap, SolidDefSnap
from dagster._utils.caching_instance_queryer import CachingInstanceQueryer

from ..implementation.fetch_assets import (
    get_freshness_info,
    get_materialization_cts_by_partition,
    get_materialization_cts_grouped_by_dimension,
)
from ..implementation.fetch_runs import AssetComputeStatus
from ..implementation.loader import BatchMaterializationLoader, CrossRepoAssetDependedByLoader
from . import external
from .asset_key import GrapheneAssetKey
from .dagster_types import GrapheneDagsterType, to_dagster_type
from .errors import GrapheneAssetNotFoundError
from .freshness_policy import GrapheneAssetFreshnessInfo, GrapheneFreshnessPolicy
from .logs.events import GrapheneMaterializationEvent
from .pipelines.pipeline import (  # GraphenePartitionMaterializationS,
    GrapheneMaterializationCountGroupedByDimension,
    GrapheneMaterializationCountSingleDimension,
    GraphenePartitionMaterializationCounts,
    GraphenePipeline,
    GrapheneRun,
)
from .util import non_null_list

if TYPE_CHECKING:
    from .external import GrapheneRepository


class GrapheneAssetDependency(graphene.ObjectType):
    class Meta:
        name = "AssetDependency"

    asset = graphene.NonNull("dagster_graphql.schema.asset_graph.GrapheneAssetNode")
    inputName = graphene.NonNull(graphene.String)

    def __init__(
        self,
        repository_location: RepositoryLocation,
        external_repository: ExternalRepository,
        input_name: Optional[str],
        asset_key: AssetKey,
        materialization_loader: Optional[BatchMaterializationLoader] = None,
        depended_by_loader: Optional[CrossRepoAssetDependedByLoader] = None,
    ):
        self._repository_location = check.inst_param(
            repository_location, "repository_location", RepositoryLocation
        )
        self._external_repository = check.inst_param(
            external_repository, "external_repository", ExternalRepository
        )
        self._asset_key = check.inst_param(asset_key, "asset_key", AssetKey)
        self._latest_materialization_loader = check.opt_inst_param(
            materialization_loader, "materialization_loader", BatchMaterializationLoader
        )
        self._depended_by_loader = check.opt_inst_param(
            depended_by_loader, "depended_by_loader", CrossRepoAssetDependedByLoader
        )
        super().__init__(inputName=input_name)

    def resolve_asset(self, _graphene_info):
        asset_node = self._external_repository.get_external_asset_node(self._asset_key)
        if not asset_node and self._depended_by_loader:
            # Only load from dependency loader if asset node cannot be found in current repository
            asset_node = self._depended_by_loader.get_sink_asset(self._asset_key)
        asset_node = check.not_none(asset_node)
        return GrapheneAssetNode(
            self._repository_location,
            self._external_repository,
            asset_node,
            self._latest_materialization_loader,
        )


class GrapheneAssetLatestInfo(graphene.ObjectType):
    assetKey = graphene.NonNull(GrapheneAssetKey)
    latestMaterialization = graphene.Field(GrapheneMaterializationEvent)
    computeStatus = graphene.NonNull(graphene.Enum.from_enum(AssetComputeStatus))
    unstartedRunIds = non_null_list(graphene.String)
    inProgressRunIds = non_null_list(graphene.String)
    latestRun = graphene.Field(GrapheneRun)

    class Meta:
        name = "AssetLatestInfo"


class GrapheneAssetNodeDefinitionCollision(graphene.ObjectType):
    assetKey = graphene.NonNull(GrapheneAssetKey)
    repositories = non_null_list(lambda: external.GrapheneRepository)

    class Meta:
        name = "AssetNodeDefinitionCollision"


class GrapheneAssetNode(graphene.ObjectType):

    _depended_by_loader: Optional[CrossRepoAssetDependedByLoader]
    _external_asset_node: ExternalAssetNode
    _node_definition_snap: Optional[Union[CompositeSolidDefSnap, SolidDefSnap]]
    _external_pipeline: Optional[ExternalPipeline]
    _external_repository: ExternalRepository
    _latest_materialization_loader: Optional[BatchMaterializationLoader]

    # NOTE: properties/resolvers are listed alphabetically
    assetKey = graphene.NonNull(GrapheneAssetKey)
    assetMaterializations = graphene.Field(
        non_null_list(GrapheneMaterializationEvent),
        partitions=graphene.List(graphene.String),
        beforeTimestampMillis=graphene.String(),
        limit=graphene.Int(),
    )
    computeKind = graphene.String()
    configField = graphene.Field(GrapheneConfigTypeField)
    dependedBy = non_null_list(GrapheneAssetDependency)
    dependedByKeys = non_null_list(GrapheneAssetKey)
    dependencies = non_null_list(GrapheneAssetDependency)
    dependencyKeys = non_null_list(GrapheneAssetKey)
    description = graphene.String()
    freshnessInfo = graphene.Field(GrapheneAssetFreshnessInfo)
    freshnessPolicy = graphene.Field(GrapheneFreshnessPolicy)
    graphName = graphene.String()
    groupName = graphene.String()
    id = graphene.NonNull(graphene.ID)
    jobNames = non_null_list(graphene.String)
    jobs = non_null_list(GraphenePipeline)
    latestMaterializationByPartition = graphene.Field(
        graphene.NonNull(graphene.List(GrapheneMaterializationEvent)),
        partitions=graphene.List(graphene.String),
    )
    partitionMaterializationCounts = graphene.Field(
        GraphenePartitionMaterializationCounts, primaryDimension=graphene.String()
    )
    metadata_entries = non_null_list(GrapheneMetadataEntry)
    op = graphene.Field(GrapheneSolidDefinition)
    opName = graphene.String()
    opNames = non_null_list(graphene.String)
    partitionDefinition = graphene.Field(GraphenePartitionDefinition)
    partitionKeys = non_null_list(graphene.String)
    partitionKeysByDimension = non_null_list(GrapheneDimensionPartitionKeys)
    repository = graphene.NonNull(lambda: external.GrapheneRepository)
    required_resources = non_null_list(GrapheneResourceRequirement)
    type = graphene.Field(GrapheneDagsterType)

    class Meta:
        name = "AssetNode"

    def __init__(
        self,
        repository_location: RepositoryLocation,
        external_repository: ExternalRepository,
        external_asset_node: ExternalAssetNode,
        materialization_loader: Optional[BatchMaterializationLoader] = None,
        depended_by_loader: Optional[CrossRepoAssetDependedByLoader] = None,
    ):
        from ..implementation.fetch_assets import get_unique_asset_id

        self._repository_location = check.inst_param(
            repository_location,
            "repository_location",
            RepositoryLocation,
        )
        self._external_repository = check.inst_param(
            external_repository, "external_repository", ExternalRepository
        )
        self._external_asset_node = check.inst_param(
            external_asset_node, "external_asset_node", ExternalAssetNode
        )
        self._latest_materialization_loader = check.opt_inst_param(
            materialization_loader, "materialization_loader", BatchMaterializationLoader
        )
        self._depended_by_loader = check.opt_inst_param(
            depended_by_loader, "depended_by_loader", CrossRepoAssetDependedByLoader
        )
        self._external_pipeline = None  # lazily loaded
        self._node_definition_snap = None  # lazily loaded

        super().__init__(
            id=get_unique_asset_id(
                external_asset_node.asset_key, repository_location.name, external_repository.name
            ),
            assetKey=external_asset_node.asset_key,
            description=external_asset_node.op_description,
            opName=external_asset_node.op_name,
            groupName=external_asset_node.group_name,
        )

    @property
    def repository_location(self) -> RepositoryLocation:
        return self._repository_location

    @property
    def external_repository(self) -> ExternalRepository:
        return self._external_repository

    @property
    def external_asset_node(self) -> ExternalAssetNode:
        return self._external_asset_node

    def get_external_pipeline(self) -> ExternalPipeline:
        if self._external_pipeline is None:
            check.invariant(
                len(self._external_asset_node.job_names) >= 1,
                "Asset must be part of at least one job",
            )
            self._external_pipeline = self._external_repository.get_full_external_job(
                self._external_asset_node.job_names[0]
            )
        return self._external_pipeline

    def get_node_definition_snap(
        self,
    ) -> Union[CompositeSolidDefSnap, SolidDefSnap]:
        if self._node_definition_snap is None and len(self._external_asset_node.job_names) > 0:
            node_key = check.not_none(
                self._external_asset_node.node_definition_name
                # nodes serialized using an older Dagster version may not have node_definition_name
                or self._external_asset_node.graph_name
                or self._external_asset_node.op_name
            )
            self._node_definition_snap = self.get_external_pipeline().get_node_def_snap(node_key)
        # weird mypy bug causes mistyped _node_definition_snap
        return check.not_none(self._node_definition_snap)  # type: ignore

    def get_partition_keys(self) -> Sequence[str]:
        # TODO: Add functionality for dynamic partitions definition
        partitions_def_data = self._external_asset_node.partitions_def_data
        if partitions_def_data:
            if (
                isinstance(partitions_def_data, ExternalStaticPartitionsDefinitionData)
                or isinstance(partitions_def_data, ExternalTimeWindowPartitionsDefinitionData)
                or isinstance(partitions_def_data, ExternalMultiPartitionsDefinitionData)
            ):
                return [
                    partition.name
                    for partition in partitions_def_data.get_partitions_definition().get_partitions()
                ]
        return []

    def is_multipartitioned(self) -> bool:
        external_multipartitions_def = self._external_asset_node.partitions_def_data

        return external_multipartitions_def is not None and isinstance(
            external_multipartitions_def, ExternalMultiPartitionsDefinitionData
        )

    def get_default_primary_dimension_name(self) -> str:
        """
        Selects a default primary partition dimension for multipartitioned assets.

        If the partitions definition has one static dimension, select that dimension. Otherwise,
        select the first dimension in the list of dimensions.
        """
        if not self.is_multipartitioned():
            check.failed(
                "Expected partitions_def_data to be an ExternalMultiPartitionsDefinitionData"
            )
        external_multipartitions_def = cast(
            ExternalMultiPartitionsDefinitionData, self._external_asset_node.partitions_def_data
        )
        static_dimensions = [
            dim
            for dim in external_multipartitions_def.external_partition_dimension_definitions
            if isinstance(dim.external_partitions_def_data, ExternalStaticPartitionsDefinitionData)
        ]
        if len(static_dimensions) == 1:
            return next(iter(static_dimensions)).name

        return next(
            iter(external_multipartitions_def.external_partition_dimension_definitions)
        ).name

    def get_partition_keys_by_dimension(self) -> Dict[str, List[str]]:
        if not self.is_multipartitioned():
            check.failed(
                "Expected partitions_def_data to be an ExternalMultiPartitionsDefinitionData"
            )

        external_multipartitions_def = cast(
            ExternalMultiPartitionsDefinitionData, self._external_asset_node.partitions_def_data
        )

        partition_keys_by_dimension: Dict[str, List[str]] = {}
        for dimension in external_multipartitions_def.external_partition_dimension_definitions:
            partition_keys_by_dimension[dimension.name] = list(
                dimension.external_partitions_def_data.get_partitions_definition().get_partition_keys()
            )
        return partition_keys_by_dimension

    def get_required_resource_keys(
        self, node_def_snap: Union[CompositeSolidDefSnap, SolidDefSnap]
    ) -> Sequence[str]:
        all_keys = self.get_required_resource_keys_rec(node_def_snap)
        return list(set(all_keys))

    def get_required_resource_keys_rec(
        self, node_def_snap: Union[CompositeSolidDefSnap, SolidDefSnap]
    ) -> Sequence[str]:
        if isinstance(node_def_snap, CompositeSolidDefSnap):
            constituent_node_names = [
                inv.solid_def_name
                for inv in node_def_snap.dep_structure_snapshot.solid_invocation_snaps
            ]
            external_pipeline = self.get_external_pipeline()
            constituent_resource_key_sets = [
                self.get_required_resource_keys_rec(external_pipeline.get_node_def_snap(name))
                for name in constituent_node_names
            ]
            return [key for res_key_set in constituent_resource_key_sets for key in res_key_set]
        else:
            return node_def_snap.required_resource_keys

    def is_graph_backed_asset(self) -> bool:
        return self.graphName is not None

    # all regular assets belong to at least one job
    def is_source_asset(self) -> bool:
        return len(self._external_asset_node.job_names) == 0

    def resolve_assetMaterializations(
        self, graphene_info, **kwargs
    ) -> Sequence[GrapheneMaterializationEvent]:
        from ..implementation.fetch_assets import get_asset_materializations

        beforeTimestampMillis: Optional[str] = kwargs.get("beforeTimestampMillis")
        try:
            before_timestamp = (
                int(beforeTimestampMillis) / 1000.0 if beforeTimestampMillis else None
            )
        except ValueError:
            before_timestamp = None

        limit = kwargs.get("limit")
        partitions = kwargs.get("partitions")
        if (
            self._latest_materialization_loader
            and limit == 1
            and not partitions
            and not before_timestamp
        ):
            latest_materialization_event = (
                self._latest_materialization_loader.get_latest_materialization_for_asset_key(
                    self._external_asset_node.asset_key
                )
            )

            if not latest_materialization_event:
                return []

            return [GrapheneMaterializationEvent(event=latest_materialization_event)]

        return [
            GrapheneMaterializationEvent(event=event)
            for event in get_asset_materializations(
                graphene_info,
                self._external_asset_node.asset_key,
                partitions,
                before_timestamp=before_timestamp,
                limit=limit,
            )
        ]

    def resolve_configField(self, _graphene_info) -> Optional[GrapheneConfigTypeField]:
        if self.is_source_asset():
            return None
        external_pipeline = self.get_external_pipeline()
        node_def_snap = self.get_node_definition_snap()
        return (
            GrapheneConfigTypeField(
                config_schema_snapshot=external_pipeline.config_schema_snapshot,
                field_snap=node_def_snap.config_field_snap,
            )
            if node_def_snap.config_field_snap
            else None
        )

    def resolve_computeKind(self, _graphene_info) -> Optional[str]:
        return self._external_asset_node.compute_kind

    def resolve_dependedBy(self, graphene_info) -> List[GrapheneAssetDependency]:
        # CrossRepoAssetDependedByLoader class loads cross-repo asset dependencies workspace-wide.
        # In order to avoid recomputing workspace-wide values per asset node, we add a loader
        # that batch loads all cross-repo dependencies for the whole workspace.
        _depended_by_loader = check.not_none(
            self._depended_by_loader,
            "depended_by_loader must exist in order to resolve dependedBy nodes",
        )

        depended_by_asset_nodes = _depended_by_loader.get_cross_repo_dependent_assets(
            self._repository_location.name,
            self._external_repository.name,
            self._external_asset_node.asset_key,
        )
        depended_by_asset_nodes.extend(self._external_asset_node.depended_by)

        if not depended_by_asset_nodes:
            return []

        materialization_loader = BatchMaterializationLoader(
            instance=graphene_info.context.instance,
            asset_keys=[dep.downstream_asset_key for dep in depended_by_asset_nodes],
        )

        return [
            GrapheneAssetDependency(
                repository_location=self._repository_location,
                external_repository=self._external_repository,
                input_name=dep.input_name,
                asset_key=dep.downstream_asset_key,
                materialization_loader=materialization_loader,
                depended_by_loader=_depended_by_loader,
            )
            for dep in depended_by_asset_nodes
        ]

    def resolve_dependedByKeys(self, _graphene_info) -> Sequence[GrapheneAssetKey]:
        # CrossRepoAssetDependedByLoader class loads all cross-repo asset dependencies workspace-wide.
        # In order to avoid recomputing workspace-wide values per asset node, we add a loader
        # that batch loads all cross-repo dependencies for the whole workspace.
        depended_by_loader = check.not_none(
            self._depended_by_loader,
            "depended_by_loader must exist in order to resolve dependedBy nodes",
        )

        depended_by_asset_nodes = depended_by_loader.get_cross_repo_dependent_assets(
            self._repository_location.name,
            self._external_repository.name,
            self._external_asset_node.asset_key,
        )
        depended_by_asset_nodes.extend(self._external_asset_node.depended_by)

        return [
            GrapheneAssetKey(path=dep.downstream_asset_key.path) for dep in depended_by_asset_nodes
        ]

    def resolve_dependencyKeys(self, _graphene_info):
        return [
            GrapheneAssetKey(path=dep.upstream_asset_key.path)
            for dep in self._external_asset_node.dependencies
        ]

    def resolve_dependencies(self, graphene_info) -> Sequence[GrapheneAssetDependency]:
        if not self._external_asset_node.dependencies:
            return []

        materialization_loader = BatchMaterializationLoader(
            instance=graphene_info.context.instance,
            asset_keys=[dep.upstream_asset_key for dep in self._external_asset_node.dependencies],
        )
        return [
            GrapheneAssetDependency(
                repository_location=self._repository_location,
                external_repository=self._external_repository,
                input_name=dep.input_name,
                asset_key=dep.upstream_asset_key,
                materialization_loader=materialization_loader,
            )
            for dep in self._external_asset_node.dependencies
        ]

    def resolve_freshnessInfo(self, graphene_info) -> Optional[GrapheneAssetFreshnessInfo]:
        if self._external_asset_node.freshness_policy:
            asset_graph = AssetGraph.from_external_assets(
                self._external_repository.get_external_asset_nodes()
            )
            return get_freshness_info(
                asset_key=self._external_asset_node.asset_key,
                freshness_policy=self._external_asset_node.freshness_policy,
                asset_graph=asset_graph,
                # in the future, we can share this same CachingInstanceQueryer across all
                # GrapheneAssetNodes which share an external repository for improved performance
                data_time_queryer=CachingInstanceQueryer(instance=graphene_info.context.instance),
            )
        return None

    def resolve_freshnessPolicy(self, _graphene_info) -> Optional[GrapheneFreshnessPolicy]:
        if self._external_asset_node.freshness_policy:
            return GrapheneFreshnessPolicy(self._external_asset_node.freshness_policy)
        return None

    def resolve_jobNames(self, _graphene_info) -> Sequence[str]:
        return self._external_asset_node.job_names

    def resolve_jobs(self, _graphene_info) -> Sequence[GraphenePipeline]:
        job_names = self._external_asset_node.job_names or []
        return [
            GraphenePipeline(self._external_repository.get_full_external_job(job_name))
            for job_name in job_names
            if self._external_repository.has_external_job(job_name)
        ]

    def resolve_latestMaterializationByPartition(
        self, graphene_info, **kwargs
    ) -> Sequence[Optional[GrapheneMaterializationEvent]]:
        from ..implementation.fetch_assets import get_asset_materializations

        get_partition = (
            lambda event: event.dagster_event.step_materialization_data.materialization.partition
        )

        partitions = kwargs.get("partitions") or self.get_partition_keys()

        events_for_partitions = get_asset_materializations(
            graphene_info,
            self._external_asset_node.asset_key,
            partitions,
        )

        latest_materialization_by_partition = {}
        for event in events_for_partitions:  # events are sorted in order of newest to oldest
            event_partition = get_partition(event)
            if event_partition not in latest_materialization_by_partition:
                latest_materialization_by_partition[event_partition] = event
            if len(latest_materialization_by_partition) == len(partitions):
                break

        # return materializations in the same order as the provided partitions, None if
        # materialization does not exist
        ordered_materializations = [
            latest_materialization_by_partition.get(partition) for partition in partitions
        ]

        return [
            GrapheneMaterializationEvent(event=event) if event else None
            for event in ordered_materializations
        ]

    def resolve_partitionMaterializationCounts(
        self, graphene_info, **kwargs
    ) -> GraphenePartitionMaterializationCounts:
        asset_key = self._external_asset_node.asset_key

        if not self.is_multipartitioned():
            partition_keys = self.get_partition_keys()
            return GrapheneMaterializationCountSingleDimension(
                materializationCounts=iter(
                    get_materialization_cts_by_partition(
                        graphene_info, asset_key, partition_keys=partition_keys
                    )
                )
            )
        else:
            primary_dimension = kwargs.get("primaryDimension")

            if primary_dimension is None:
                primary_dimension = self.get_default_primary_dimension_name()

            partition_keys_by_dimension = self.get_partition_keys_by_dimension()
            secondary_dimension = next(
                iter(list(set(partition_keys_by_dimension.keys()) - {primary_dimension}))
            )

            return GrapheneMaterializationCountGroupedByDimension(
                materializationCountsGrouped=get_materialization_cts_grouped_by_dimension(
                    graphene_info,
                    asset_key,
                    [primary_dimension, secondary_dimension],
                    partition_keys_by_dimension,
                )
            )

    def resolve_metadata_entries(self, _graphene_info) -> Sequence[GrapheneMetadataEntry]:
        return list(iterate_metadata_entries(self._external_asset_node.metadata_entries))

    def resolve_op(
        self, _graphene_info
    ) -> Optional[Union[GrapheneSolidDefinition, GrapheneCompositeSolidDefinition]]:
        if self.is_source_asset():
            return None
        external_pipeline = self.get_external_pipeline()
        node_def_snap = self.get_node_definition_snap()
        if isinstance(node_def_snap, SolidDefSnap):
            return GrapheneSolidDefinition(external_pipeline, node_def_snap.name)

        if isinstance(node_def_snap, CompositeSolidDefSnap):
            return GrapheneCompositeSolidDefinition(external_pipeline, node_def_snap.name)

        check.failed(f"Unknown solid definition type {type(node_def_snap)}")

    def resolve_opNames(self, _graphene_info) -> Sequence[str]:
        return self._external_asset_node.op_names or []

    def resolve_graphName(self, _graphene_info) -> Optional[str]:
        return self._external_asset_node.graph_name

    def resolve_partitionKeysByDimension(
        self, _graphene_info
    ) -> Sequence[GrapheneDimensionPartitionKeys]:
        if not self.is_multipartitioned():
            return [
                GrapheneDimensionPartitionKeys(
                    name="default", partition_keys=self.get_partition_keys()
                )
            ]

        return [
            GrapheneDimensionPartitionKeys(name=dimension_name, partition_keys=partition_keys)
            for dimension_name, partition_keys in self.get_partition_keys_by_dimension().items()
        ]

    def resolve_partitionKeys(self, _graphene_info) -> Sequence[str]:
        return self.get_partition_keys()

    def resolve_partitionDefinition(self, _graphene_info) -> Optional[GraphenePartitionDefinition]:
        partitions_def_data = self._external_asset_node.partitions_def_data
        if partitions_def_data:
            return GraphenePartitionDefinition(partitions_def_data)
        return None

    def resolve_repository(self, graphene_info) -> "GrapheneRepository":
        return external.GrapheneRepository(
            graphene_info.context.instance, self._external_repository, self._repository_location
        )

    def resolve_required_resources(self, _graphene_info) -> Sequence[GrapheneResourceRequirement]:
        if self.is_source_asset():
            return []
        node_def_snap = self.get_node_definition_snap()
        all_unique_keys = self.get_required_resource_keys(node_def_snap)
        return [GrapheneResourceRequirement(key) for key in all_unique_keys]

    def resolve_type(self, _graphene_info) -> Optional[str]:
        if self.is_source_asset():
            return None
        external_pipeline = self.get_external_pipeline()
        output_name = self.external_asset_node.output_name
        if output_name:
            for output_def in self.get_node_definition_snap().output_def_snaps:
                if output_def.name == output_name:
                    return to_dagster_type(
                        external_pipeline.pipeline_snapshot,
                        output_def.dagster_type_key,
                    )
        return None


class GrapheneAssetGroup(graphene.ObjectType):
    groupName = graphene.NonNull(graphene.String)
    assetKeys = non_null_list(GrapheneAssetKey)

    class Meta:
        name = "AssetGroup"


class GrapheneAssetNodeOrError(graphene.Union):
    class Meta:
        types = (GrapheneAssetNode, GrapheneAssetNotFoundError)
        name = "AssetNodeOrError"
