"""A wrapper over the ``samples`` and ``sample_positions`` collections."""

import re
from datetime import datetime
from enum import Enum, auto
from typing import Any, Dict, List, Optional, Tuple, Union, cast

import pymongo  # type: ignore
from bson import ObjectId  # type: ignore
from pydantic import BaseModel, conint

from alab_management.utils.data_objects import get_collection, get_lock

from .sample import Sample, SamplePosition


class SamplePositionRequest(BaseModel):
    """
    The class is used to request sample position.

    You need to specify the prefix of the sample position (will be used to match by `startwith` method) and
    the number you request. By default, the number is set to be 1.
    """

    class Config:
        """raise error when extra kwargs."""

        extra = "forbid"

    prefix: str
    number: conint(ge=0) = 1  # type: ignore

    @classmethod
    def from_str(cls, sample_position_prefix: str) -> "SamplePositionRequest":
        """Create a ``SamplePositionRequest`` from a string."""
        return cls(prefix=sample_position_prefix)

    @classmethod
    def from_py_type(cls, sample_position: Union[str, Dict[str, Any]]):
        """Create a ``SamplePositionRequest`` from a string or a dict."""
        if isinstance(sample_position, str):
            return cls.from_str(sample_position_prefix=sample_position)
        return cls(**sample_position)


class SamplePositionStatus(Enum):
    """
    The status of a sample position.

    - ``EMPTY``: the sample position is neither locked nor occupied
    - ``OCCUPIED``: there is a sample in the sample position
    - ``LOCKED``: the sample position is reserved by a task
    """

    EMPTY = auto()
    OCCUPIED = auto()
    LOCKED = auto()


class SampleView:
    """Sample view manages the samples and their positions."""

    def __init__(self):
        self._sample_collection = get_collection("samples")
        self._sample_positions_collection = get_collection("sample_positions")
        self._sample_positions_collection.create_index(
            [
                (
                    "name",
                    pymongo.HASHED,
                )
            ]
        )
        self._lock = get_lock(self._sample_positions_collection.name)

    def add_sample_positions_to_db(
        self,
        sample_positions: List[SamplePosition],
        parent_device_name: Optional[str] = None,
    ):
        """
        Insert sample positions info to db, which includes position name and description.

        If one sample position's name has already appeared in the database,
        we will just skip it.

        Args:
            sample_positions: some sample position instances
            parent_device_name: name of the parent device to these sample_positions.
        """
        for sample_pos in sample_positions:
            for i in range(sample_pos.number):
                # we use <name><SEPARATOR><number> format to create multiple sample positions
                # if there is only one sample position (sample_position.number == 1)
                # the name of sample position will be directly used as the sample position's name in the database
                name = (
                    f"{sample_pos.name}{SamplePosition.SEPARATOR}{i+1}"  # index from 1
                    if sample_pos.number != 1
                    else sample_pos.name
                )
                if parent_device_name:
                    name = f"{parent_device_name}{SamplePosition.SEPARATOR}{name}"

                if re.search(r"[$.]", name) is not None:
                    raise ValueError(
                        f"Unsupported sample position name: {name}. " f"Sample position name should not contain '.' or '$'"
                    )

                sample_pos_ = self._sample_positions_collection.find_one({"name": name})
                if sample_pos_ is None:
                    new_entry = {
                        "name": name,
                        "description": sample_pos.description,
                        "task_id": None,
                        "last_updated": datetime.now(),
                    }
                    if parent_device_name:
                        new_entry["parent_device"] = parent_device_name
                    self._sample_positions_collection.insert_one(new_entry)

    def clean_up_sample_position_collection(self):
        """Drop the sample position collection."""
        self._sample_positions_collection.drop()

    def request_sample_positions(
        self,
        task_id: ObjectId,
        sample_positions: List[Union[SamplePositionRequest, str, Dict[str, Any]]],
    ) -> Optional[Dict[str, List[Dict[str, Any]]]]:
        """
        Request a list of sample positions, this function will return until all the sample positions are available.

        Args:
            task_id: the task id that requests these resources
            sample_positions: the list of sample positions, which is requested by their names.
              The sample position name is actually the prefix of a sample position, which we
              will try to match all the sample positions will the name
        """
        sample_positions_request: List[SamplePositionRequest] = [
            SamplePositionRequest.from_py_type(sample_position)
            if not isinstance(sample_position, SamplePositionRequest)
            else sample_position
            for sample_position in sample_positions
        ]

        if len(sample_positions_request) != len({sample_position.prefix for sample_position in sample_positions_request}):
            raise ValueError("Duplicated sample_positions in one request.")

        # check if there are enough positions
        for sample_position in sample_positions_request:
            count = self._sample_positions_collection.count_documents(
                {"name": {"$regex": f"^{re.escape(sample_position.prefix)}"}}
            )
            if count < sample_position.number:
                raise ValueError(
                    f"Position prefix `{sample_position.prefix}` can only "
                    f"have {count} matches, but requests {sample_position.number}"
                )

        with self._lock():  # pylint: disable=not-callable
            available_positions: Dict[str, List[Dict[str, Union[str, bool]]]] = {}
            for sample_position in sample_positions_request:
                result = self.get_available_sample_position(task_id, position_prefix=sample_position.prefix)
                if not result or len(result) < sample_position.number:
                    return None
                # we try to choose the position that has already been locked by this task
                available_positions[sample_position.prefix] = sorted(result, key=lambda task: int(task["need_release"]))[
                    : sample_position.number
                ]
            return available_positions

    def get_sample_position(self, position: str) -> Optional[Dict[str, Any]]:
        """
        Get the sample position entry in the database.

        if the position is not a valid position (not defined in the database), return None
        """
        return self._sample_positions_collection.find_one({"name": position})

    def get_sample_position_status(self, position: str) -> Tuple[SamplePositionStatus, Optional[ObjectId]]:
        """
        Get the status of a sample position.

        If there is a sample in the position, return OCCUPIED;
        else if the sample position is locked by a task, return LOCKED;
        return EMPTY

        Args:
            position: the name of the sample position

        Returns
        -------
            if the position is occupied by a sample, return OCCUPIED and the task id of the sample
            if the position is locked by a task, return LOCKED and the task id
            else, return EMPTY and None
        """
        sample_position = self.get_sample_position(position=position)
        if sample_position is None:
            raise ValueError(f"Invalid sample position: {position}")

        sample = self._sample_collection.find_one({"position": position})
        if sample is not None:
            return SamplePositionStatus.OCCUPIED, sample["task_id"]

        if sample_position["task_id"] is not None:
            return SamplePositionStatus.LOCKED, sample_position["task_id"]

        return SamplePositionStatus.EMPTY, None

    def get_sample_position_parent_device(self, position: str) -> Optional[str]:
        """
        Get the parent device of a sample position.

        If no parent device is defined, returns None. If the
        "position" query is a prefix, will look for a single parent device across all matched positions (ie a query
        for position="furnace_1/tray" will properly return "furnace_1" even if "furnace_1/tray/1" and
        "furnace_1/tray/2" are in the database _as long as "furnace_1" is the parent device of both!_).
        """
        sample_positions = self._sample_positions_collection.find({"name": {"$regex": f"^{position}"}})
        parent_devices = list({sp.get("parent_device") for sp in sample_positions})
        if len(parent_devices) == 0:
            raise ValueError(f"No sample position(s) beginning with: {position}")
        elif len(parent_devices) > 1:
            raise Exception(
                f"Multiple parent devices ({parent_devices}) found for sample positions found beginning with: "
                f'"position". Make a more specific position query that doesn\'t match multiple devices!'
            )
        return parent_devices[0]

    def is_unoccupied_position(self, position: str) -> bool:
        """Tell if a sample position is unoccupied or not."""
        return self.get_sample_position_status(position)[0] is not SamplePositionStatus.OCCUPIED

    def get_available_sample_position(self, task_id: ObjectId, position_prefix: str) -> List[Dict[str, Union[str, bool]]]:
        """
        Check if the position is occupied.

        The structure of returned list is ``{"name": str, "need_release": bool}``.
        The entry need_release indicates whether a sample position needs to be released
        when __exit__ method is called in the ``SamplePositionsLock``.
        """
        if self._sample_positions_collection.find_one({"name": {"$regex": f"^{re.escape(position_prefix)}"}}) is None:
            raise ValueError(f"Cannot find device with prefix: {position_prefix}")

        available_sample_positions = self._sample_positions_collection.find(
            {
                "name": {"$regex": f"^{re.escape(position_prefix)}"},
                "$or": [
                    {
                        "task_id": None,
                    },
                    {
                        "task_id": task_id,
                    },
                ],
            }
        )
        available_sp_names = []
        for sample_position in available_sample_positions:
            status, current_task_id = self.get_sample_position_status(sample_position["name"])
            if status is SamplePositionStatus.EMPTY or task_id == current_task_id:
                available_sp_names.append(
                    {
                        "name": sample_position["name"],
                        "need_release": self.get_sample_position(sample_position["name"])["task_id"] != task_id,  # type: ignore
                    }
                )
        return available_sp_names

    def lock_sample_position(self, task_id: ObjectId, position: str):
        """Lock a sample position."""
        sample_status, current_task_id = self.get_sample_position_status(position)

        if current_task_id != task_id:
            if sample_status is SamplePositionStatus.OCCUPIED:
                raise ValueError(f"Position ({position}) is currently occupied")
            if sample_status is SamplePositionStatus.LOCKED:
                raise ValueError(f"Position is currently locked by task: {current_task_id}")

        self._sample_positions_collection.update_one(
            {"name": position},
            {
                "$set": {
                    "task_id": task_id,
                }
            },
        )

    def release_sample_position(self, position: str):
        """Unlock a sample position."""
        if self.get_sample_position(position) is None:
            raise ValueError(f"Invalid sample position: {position}")

        self._sample_positions_collection.update_one(
            {"name": position},
            {
                "$set": {
                    "task_id": None,
                }
            },
        )

    def get_sample_positions_by_task(self, task_id: Optional[ObjectId]) -> List[str]:
        """Get the list of sample positions that is locked by a task (given task id)."""
        return [sample_position["name"] for sample_position in self._sample_positions_collection.find({"task_id": task_id})]

    #################################################################
    #                 operations related to samples                 #
    #################################################################

    def create_sample(
        self,
        name: str,
        position: Optional[str] = None,
        sample_id: Optional[ObjectId] = None,
        tags: Optional[List[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> ObjectId:
        """
        Create a sample and return its uid in the database.

        Samples with the same name can exist in the database
        """
        if position is not None and not self.is_unoccupied_position(position):
            raise ValueError(f"Requested position ({position}) is not EMPTY.")

        if re.search(r"[.$]", name) is not None:
            raise ValueError(f"Unsupported sample name: {name}. " f"Sample name should not contain '.' or '$'")

        entry = {
            "name": name,
            "tags": tags or [],
            "metadata": metadata or {},
            "position": position,
            "task_id": None,
            "created_at": datetime.now(),
            "last_updated": datetime.now(),
        }
        if sample_id:
            if not isinstance(sample_id, ObjectId):
                raise ValueError(
                    f"User provided {sample_id} as the sample_id -- this is not a valid ObjectId, so this sample "
                    f"cannot be created in the database!"
                )
            entry["_id"] = sample_id

        result = self._sample_collection.insert_one(entry)

        return cast(ObjectId, result.inserted_id)

    def get_sample(self, sample_id: ObjectId) -> Sample:
        """Get a sample by its id.

        Args:
            sample_id (ObjectId): id of the sample within sample collection

        Raises
        ------
            ValueError: no sample found with given id

        Returns
        -------
            Sample: Sample object for given id
        """
        result = self._sample_collection.find_one({"_id": sample_id})
        if result is None:
            raise ValueError(f"No sample found with id: {sample_id}")

        return Sample(
            sample_id=result["_id"],
            name=result["name"],
            position=result["position"],
            task_id=result["task_id"],
            metadata=result.get("metadata", {}),
            tags=result.get("tags", []),
        )

    def update_sample_task_id(self, sample_id: ObjectId, task_id: Optional[ObjectId]):
        """Update the task id for a sample."""
        result = self._sample_collection.find_one({"_id": sample_id})
        if result is None:
            raise ValueError(f"Cannot find sample with id: {sample_id}")

        self._sample_collection.update_one(
            {"_id": sample_id},
            {
                "$set": {
                    "task_id": task_id,
                    "last_updated": datetime.now(),
                }
            },
        )

    def move_sample(self, sample_id: ObjectId, position: Optional[str]):
        """Update the sample with new position."""
        result = self._sample_collection.find_one({"_id": sample_id})
        if result is None:
            raise ValueError(f"Cannot find sample with id: {sample_id}")

        if result["position"] == position:
            return

        if position is not None and not self.is_unoccupied_position(position):
            raise ValueError(f"Requested position ({position}) is not EMPTY or LOCKED by other task.")

        self._sample_collection.update_one(
            {"_id": sample_id},
            {
                "$set": {
                    "position": position,
                    "last_updated": datetime.now(),
                }
            },
        )

    def exists(self, sample_id: Union[ObjectId, str]) -> bool:
        """Check if a sample exists in the database.

        Args:
            sample_id (ObjectId): id of the sample within sample collection

        Returns
        -------
            bool: True if sample exists in the database
        """
        return self._sample_collection.count_documents({"_id": ObjectId(sample_id)}) > 0
