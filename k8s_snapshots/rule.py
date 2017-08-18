from datetime import timedelta
from typing import Dict, Any, List, Union, Iterable

import attr
import isodate
import pykube
import structlog

from k8s_snapshots import kube
from k8s_snapshots.context import Context
from k8s_snapshots.errors import (
    UnsupportedVolume,
    AnnotationNotFound,
    AnnotationError,
    DeltasParseError
)
from k8s_snapshots.logging import Loggable
from k8s_snapshots.backends import find_backend_for_volume, get_backend
from k8s_snapshots.backends.abstract import DiskIdentifier

_logger = structlog.get_logger(__name__)


@attr.s(slots=True)
class Rule(Loggable):
    """
    A rule describes how and when to make backups.
    """
    name = attr.ib()
    deltas = attr.ib()
    backend = attr.ib()
    disk = attr.ib()

    #: For Kubernetes resources: The selfLink of the source
    source = attr.ib(default=None)

    @classmethod
    def from_volume(
            cls,
            volume: pykube.objects.PersistentVolume,
            backend: str,
            disk: DiskIdentifier,
            source: Union[
                pykube.objects.PersistentVolumeClaim,
                pykube.objects.PersistentVolume
            ],
            deltas: List[timedelta],
            use_claim_name: bool=False
    ) -> 'Rule':

        claim_name = ""
        if use_claim_name:
            claim_ref = volume.obj['spec'].get('claimRef')
            if claim_ref:
                claim_name = claim_ref.get('name')

        return cls(
            name=rule_name_from_k8s_source(source, claim_name),
            backend=backend,
            source=source.obj['metadata']['selfLink'],
            deltas=deltas,
            disk=disk
        )

    def to_dict(self) -> Dict[str, Any]:
        return attr.asdict(self)


def get_backend_for_rule(ctx: Context, rule: Rule):
    return get_backend(rule.backend)


def rule_name_from_k8s_source(
        source: Union[
            pykube.objects.PersistentVolumeClaim,
            pykube.objects.PersistentVolume
        ],
        name: str = False
) -> str:
    short_kind = {
        'PersistentVolume': 'pv',
        'PersistentVolumeClaim': 'pvc',
    }.pop(source.kind)

    source_namespace = source.namespace

    # PV's have a namespace set to an empty string ''
    if source_namespace == 'default' or not source_namespace:
        namespace = ''
    else:
        namespace = f'{source.namespace}-'

    if not name:
        name = source.name
    rule_name = f'{namespace}{short_kind}-{name}'

    _logger.debug(
        'rule-name-from-k8s',
        key_hints=[
            'source_namespace',
            'source.kind',
            'source.metadata.namespace',
            'source.metadata.name',
            'rule_name',
        ],
        source_namespace=source_namespace,
        source=source.obj,
        rule_name=rule_name,
    )
    return rule_name


def parse_deltas(
        delta_string: str
) -> List[Union[timedelta, isodate.Duration]]:
    """q§Parse the given string into a list of ``timedelta`` instances.
    """
    if delta_string is None:
        raise DeltasParseError(
            f'Delta string is None',
        )

    deltas = []
    for item in delta_string.split(' '):
        item = item.strip()
        if not item:
            continue
        try:
            deltas.append(isodate.parse_duration(item))
        except ValueError as exc:
            raise DeltasParseError(
                f'Could not parse duration: {item!r}',
                error=exc,
                item=item,
                deltas=deltas,
                delta_string=delta_string,
            ) from exc

    if deltas and len(deltas) < 2:
        raise DeltasParseError(
            'At least two deltas are required',
            deltas=deltas,
            delta_string=delta_string,
        )

    return deltas


def serialize_deltas(deltas: Iterable[timedelta]) -> str:
    delta_strs = [
        isodate.duration_isoformat(delta)
        for delta in deltas
    ]
    return ' '.join(delta_strs)


async def rule_from_pv(
    ctx: Context,
    volume: pykube.objects.PersistentVolume,
    deltas_annotation_key: str,
    use_claim_name: bool=False,
) -> Rule:
    """Given a persistent volume object, create a backup role
    object. Can return None if this volume is not configured for
    backups, or is not suitable.

    Parameters

    `use_claim_name` - if the persistent volume is bound, and it's
    name is auto-generated, then prefer to use the name of the claim
    for the snapshot.
    """
    _log = _logger.new(
        volume=volume.obj,
        annotation_key=deltas_annotation_key,
    )

    # Do we have a backend that supports this disk?
    backend_name, backend_module = find_backend_for_volume(volume)
    if not backend_module:
        raise UnsupportedVolume(
            'Unsupported volume',
            volume=volume
        )
    else:
        disk = backend_module.get_disk_identifier(volume)
        _log.debug('Volume supported by backend',
                   volume=volume, backend=backend_module, disk=disk)

    def get_deltas(annotations: Dict) -> List[timedelta]:
        """
        Helper annotation-deltas-getter

        Parameters
        ----------
        annotations

        Returns
        -------

        """
        try:
            deltas_str = annotations[deltas_annotation_key]
        except KeyError as exc:
            raise AnnotationNotFound(
                'No such annotation key',
                key=deltas_annotation_key
            ) from exc

        if not deltas_str:
            raise AnnotationError('Invalid delta string', deltas_str=deltas_str)

        try:
            deltas = parse_deltas(deltas_str)
        except DeltasParseError as exc:
            raise AnnotationError(
                'Invalid delta string',
                deltas_str=deltas_str
            ) from exc

        if deltas is None or not deltas:
            raise AnnotationError(
                'parse_deltas returned invalid deltas',
                deltas_str=deltas_str,
                deltas=deltas,
            )

        return deltas

    claim_ref = volume.obj['spec'].get('claimRef')

    try:
        _log.debug('Checking volume for deltas')
        deltas = get_deltas(volume.annotations)
        return Rule.from_volume(volume, backend_name, disk=disk,
            source=volume, deltas=deltas, use_claim_name=use_claim_name)
    except AnnotationNotFound:
        if claim_ref is None:
            raise

    volume_claim = await kube.get_resource_or_none(
        ctx.kube_client,
        pykube.objects.PersistentVolumeClaim,
        claim_ref['name'],
        namespace=claim_ref['namespace'],
    )

    if volume_claim is None:
        raise AnnotationError(
            'Could not find the PersistentVolumeClaim from claim_ref',
            claim_ref=claim_ref,
        )

    try:
        _log.debug('Checking volume claim for deltas')
        deltas = get_deltas(volume_claim.annotations)
        return Rule.from_volume(volume, backend_name, disk=disk,
            source=volume_claim, deltas=deltas, use_claim_name=use_claim_name)
    except AnnotationNotFound as exc:
        raise AnnotationNotFound(
            'No deltas found via volume claim'
        ) from exc

