"""
Ethereum state test spec definition and filler.
"""

from copy import copy
from dataclasses import dataclass
from typing import Callable, Generator, List, Mapping, Optional, Type

from ethereum_test_forks import Cancun, Fork
from evm_transition_tool import FixtureFormats, TransitionTool

from ...common import Account, Address, Alloc, Environment, Number, Transaction
from ...common.constants import EngineAPIError
from ...common.json import to_json
from ..base.base_test import BaseFixture, BaseTest, verify_post_alloc
from ..blockchain.blockchain_test import Block, BlockchainTest
from ..blockchain.types import Header
from ..debugging import print_traces
from .types import Fixture, FixtureForkPost

BEACON_ROOTS_ADDRESS = Address(0x000F3DF6D732807EF1319FB7B8BB8522D0BEAC02)
TARGET_BLOB_GAS_PER_BLOCK = 393216


@dataclass(kw_only=True)
class StateTest(BaseTest):
    """
    Filler type that tests transactions over the period of a single block.
    """

    env: Environment
    pre: Mapping
    post: Mapping
    tx: Transaction
    engine_api_error_code: Optional[EngineAPIError] = None
    blockchain_test_header_verify: Optional[Header] = None
    blockchain_test_rlp_modifier: Optional[Header] = None
    tag: str = ""
    chain_id: int = 1

    @classmethod
    def pytest_parameter_name(cls) -> str:
        """
        Returns the parameter name used to identify this filler in a test.
        """
        return "state_test"

    @classmethod
    def fixture_formats(cls) -> List[FixtureFormats]:
        """
        Returns a list of fixture formats that can be output to the test spec.
        """
        return [
            FixtureFormats.BLOCKCHAIN_TEST,
            FixtureFormats.BLOCKCHAIN_TEST_HIVE,
            FixtureFormats.STATE_TEST,
        ]

    def _generate_blockchain_genesis_environment(self) -> Environment:
        """
        Generate the genesis environment for the BlockchainTest formatted test.
        """
        genesis_env = copy(self.env)

        # Modify values to the proper values for the genesis block
        # TODO: All of this can be moved to a new method in `Fork`
        genesis_env.withdrawals = None
        genesis_env.beacon_root = None
        genesis_env.number = Number(genesis_env.number) - 1
        assert (
            genesis_env.number >= 0
        ), "genesis block number cannot be negative, set state test env.number to 1"
        if genesis_env.excess_blob_gas:
            # The excess blob gas environment value means the value of the context (block header)
            # where the transaction is executed. In a blockchain test, we need to indirectly
            # set the excess blob gas by setting the excess blob gas of the genesis block
            # to the expected value plus the TARGET_BLOB_GAS_PER_BLOCK, which is the value
            # that will be subtracted from the excess blob gas when the first block is mined.
            genesis_env.excess_blob_gas = (
                Number(genesis_env.excess_blob_gas) + TARGET_BLOB_GAS_PER_BLOCK
            )

        return genesis_env

    def _generate_blockchain_blocks(self) -> List[Block]:
        """
        Generate the single block that represents this state test in a BlockchainTest format.
        """
        return [
            Block(
                number=self.env.number,
                timestamp=self.env.timestamp,
                coinbase=self.env.coinbase,
                difficulty=self.env.difficulty,
                gas_limit=self.env.gas_limit,
                extra_data=self.env.extra_data,
                withdrawals=self.env.withdrawals,
                beacon_root=self.env.beacon_root,
                txs=[self.tx],
                ommers=[],
                exception=self.tx.error,
                header_verify=self.blockchain_test_header_verify,
                rlp_modifier=self.blockchain_test_rlp_modifier,
            )
        ]

    def generate_blockchain_test(self) -> BlockchainTest:
        """
        Generate a BlockchainTest fixture from this StateTest fixture.
        """
        return BlockchainTest(
            genesis_environment=self._generate_blockchain_genesis_environment(),
            pre=self.pre,
            post=self.post,
            blocks=self._generate_blockchain_blocks(),
            fixture_format=self.fixture_format,
            t8n_dump_dir=self.t8n_dump_dir,
        )

    def make_state_test_fixture(
        self,
        t8n: TransitionTool,
        fork: Fork,
        eips: Optional[List[int]] = None,
    ) -> Fixture:
        """
        Create a fixture from the state test definition.
        """
        env = self.env.set_fork_requirements(fork)
        tx = self.tx.with_signature_and_sender(keep_secret_key=True)
        pre_alloc = Alloc.merge(
            Alloc(
                fork.pre_allocation(block_number=env.number, timestamp=Number(env.timestamp)),
            ),
            Alloc(self.pre),
        )
        if empty_accounts := pre_alloc.empty_accounts():
            raise Exception(f"Empty accounts in pre state: {empty_accounts}")
        transition_tool_name = fork.transition_tool_name(
            block_number=Number(self.env.number),
            timestamp=Number(self.env.timestamp),
        )
        fork_name = (
            "+".join([transition_tool_name] + [str(eip) for eip in eips])
            if eips
            else transition_tool_name
        )
        next_alloc, result = t8n.evaluate(
            alloc=to_json(pre_alloc),
            txs=to_json([tx]),
            env=to_json(env),
            fork_name=fork_name,
            chain_id=self.chain_id,
            reward=0,  # Reward on state tests is always zero
            eips=eips,
            debug_output_path=self.get_next_transition_tool_output_path(),
        )

        try:
            verify_post_alloc(self.post, next_alloc)
        except Exception as e:
            print_traces(t8n.get_traces())
            raise e

        # Perform post state processing required for some forks
        if fork >= Cancun:
            # StateTest does not execute any beacon root contract logic, but we still need to
            # set the beacon root to the correct value, because most tests assume this happens,
            # so we copy the beacon root contract storage from the post state into the pre state
            # and the transaction is executed in isolation properly.
            if beacon_roots_account := next_alloc.get(str(BEACON_ROOTS_ADDRESS)):
                if beacon_roots_storage := beacon_roots_account.get("storage"):
                    pre_alloc = Alloc.merge(
                        pre_alloc,
                        Alloc({BEACON_ROOTS_ADDRESS: Account(storage=beacon_roots_storage)}),
                    )

        return Fixture(
            env=env,
            pre_state=pre_alloc,
            post={
                fork.blockchain_test_network_name(): [
                    FixtureForkPost.collect(
                        transition_tool_result=result,
                        transaction=tx.with_signature_and_sender(),
                    )
                ]
            },
            transaction=tx,
        )

    def generate(
        self,
        t8n: TransitionTool,
        fork: Fork,
        eips: Optional[List[int]] = None,
    ) -> BaseFixture:
        """
        Generate the BlockchainTest fixture.
        """
        if self.fixture_format in BlockchainTest.fixture_formats():
            return self.generate_blockchain_test().generate(t8n, fork, eips)
        elif self.fixture_format == FixtureFormats.STATE_TEST:
            # We can't generate a state test fixture that names a transition fork,
            # so we get the fork at the block number and timestamp of the state test
            fork = fork.fork_at(Number(self.env.number), Number(self.env.timestamp))
            return self.make_state_test_fixture(t8n, fork, eips)

        raise Exception(f"Unknown fixture format: {self.fixture_format}")


class StateTestOnly(StateTest):
    """
    StateTest filler that only generates a state test fixture.
    """

    @classmethod
    def pytest_parameter_name(cls) -> str:
        """
        Returns the parameter name used to identify this filler in a test.
        """
        return "state_test_only"

    @classmethod
    def fixture_formats(cls) -> List[FixtureFormats]:
        """
        Returns a list of fixture formats that can be output to the test spec.
        """
        return [FixtureFormats.STATE_TEST]


StateTestSpec = Callable[[str], Generator[StateTest, None, None]]
StateTestFiller = Type[StateTest]
