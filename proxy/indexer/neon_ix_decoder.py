import logging
import dataclasses
import abc
from typing import Any, List, Type, Optional, Iterator

from ..common_neon.utils import NeonTxInfo
from ..common_neon.evm_log_decoder import NeonLogTxEvent

from ..indexer.indexed_objects import NeonIndexedTxInfo, NeonIndexedHolderInfo, NeonAccountInfo, SolNeonTxDecoderState


LOG = logging.getLogger(__name__)


class DummyIxDecoder:
    _name = 'Unknown'
    _ix_code = 0xFF
    _is_deprecated = True

    def __init__(self, state: SolNeonTxDecoderState):
        self._state = state
        LOG.debug(f'{self} ...')

    @classmethod
    def ix_code(cls) -> int:
        return cls._ix_code

    @classmethod
    def is_deprecated(cls) -> bool:
        return cls._is_deprecated

    @classmethod
    def name(cls) -> str:
        return cls._name

    def __str__(self):
        if self._is_deprecated:
            return f'DEPRECATED 0x{self._ix_code:02x}:{self._name} {self.state.sol_neon_ix}'
        return f'0x{self._ix_code:02x}:{self._name} {self.state.sol_neon_ix}'

    def execute(self) -> bool:
        """By default, skip the instruction without parsing."""
        ix = self.state.sol_neon_ix
        return self._decoding_skip(f'no logic to decode the instruction {self}({ix.ix_data.hex()[:8]})')

    @property
    def state(self) -> SolNeonTxDecoderState:
        return self._state

    @staticmethod
    def _decoding_success(indexed_obj: Any, msg: str) -> bool:
        """
        The instruction has been successfully parsed:
        - log the success message.
        """
        LOG.debug(f'decoding success: {msg} - {indexed_obj}')
        return True

    def _decoding_done(self, indexed_obj: Any, msg: str) -> bool:
        """
        Assembling of the object has been successfully finished.
        """
        ix = self.state.sol_neon_ix
        block = self.state.neon_block
        if isinstance(indexed_obj, NeonIndexedTxInfo):
            block.done_neon_tx(indexed_obj, ix)
        elif isinstance(indexed_obj, NeonIndexedHolderInfo):
            block.done_neon_holder(indexed_obj)
        LOG.debug(f'decoding done: {msg} - {indexed_obj}')
        return True

    @staticmethod
    def _decoding_skip(reason: str) -> bool:
        """Skip decoding of the instruction"""
        LOG.warning(f'decoding skip: {reason}')
        return False

    def _decode_neon_tx_from_holder(self, tx: NeonIndexedTxInfo) -> None:
        if tx.neon_tx.is_valid() or (not tx.neon_tx_res.is_valid()):
            return
        t = NeonIndexedTxInfo.Type
        if tx.tx_type not in {t.SingleFromAccount, t.IterFromAccount, t.IterFromAccountWoChainId}:
            return

        key = NeonIndexedHolderInfo.Key(tx.holder_account, tx.neon_tx.sig)
        holder = self.state.neon_block.find_neon_tx_holder(key, self.state.sol_neon_ix)
        if holder is None:
            return

        neon_tx = NeonTxInfo.from_sig_data(holder.data)
        if not neon_tx.is_valid():
            self._decoding_skip(f'Neon tx rlp error: {neon_tx.error}')
        elif holder.neon_tx_sig != neon_tx.sig[2:]:
            # failed decoding ...
            self._decoding_skip(f'Neon tx hash {neon_tx.sig} != holder hash {holder.neon_tx_sig}')
        elif neon_tx.sig != tx.neon_tx.sig:
            # failed decoding ...
            self._decoding_skip(f'Neon tx hash {neon_tx.sig} != tx log hash {tx.neon_tx.sig}')
        else:
            tx.set_neon_tx(neon_tx, holder)
            self._decoding_done(holder, f'init Neon tx {tx.neon_tx} from holder')

    def _decode_neon_tx_return(self, tx: NeonIndexedTxInfo) -> None:
        res = tx.neon_tx_res
        if res.is_valid():
            return

        ix = self.state.sol_neon_ix
        ret = ix.neon_tx_return
        if ret is not None:
            res.set_result(status=ret.status, gas_used=ret.gas_used)
        elif self._decode_neon_tx_cancel_return(tx):
            pass
        elif self._decode_neon_tx_lost_return(tx):
            pass
        else:
            return

        res.set_sol_sig_info(ix.sol_sig, ix.idx, ix.inner_idx)
        event_type = NeonLogTxEvent.Type.Cancel if tx.is_canceled else NeonLogTxEvent.Type.Return

        tx.add_neon_event(NeonLogTxEvent(
            event_type=event_type, is_hidden=True, address=b'', topic_list=list(),
            data=int(res.status[2:], 16).to_bytes(1, 'little'),
            total_gas_used=int(res.gas_used[2:], 16) + 5000,  # to move event to the end of the list
            sol_sig=ix.sol_sig, idx=ix.idx, inner_idx=ix.inner_idx
        ))

    def _decode_neon_tx_cancel_return(self, tx: NeonIndexedTxInfo) -> bool:
        tp = NeonIndexedTxInfo.Type
        if tx.tx_type in {tp.SingleFromAccount, tp.Single}:
            return False

        ix = self.state.sol_neon_ix
        if not tx.is_canceled:
            return False

        tx.neon_tx_res.set_canceled_result(ix.neon_total_gas_used)
        return True

    def _decode_neon_tx_lost_return(self, tx: NeonIndexedTxInfo) -> bool:
        return False

    def _decode_neon_tx_event_list(self, tx: NeonIndexedTxInfo) -> None:
        total_gas_used = self.state.sol_neon_ix.neon_total_gas_used
        for event in self.state.sol_neon_ix.neon_tx_event_list:
            tx.add_neon_event(dataclasses.replace(
                event,
                total_gas_used=total_gas_used,
                sol_sig=self.state.sol_neon_ix.sol_sig,
                idx=self.state.sol_neon_ix.idx,
                inner_idx=self.state.sol_neon_ix.inner_idx
            ))
            total_gas_used += 1

    def _decode_tx(self, tx: NeonIndexedTxInfo, msg: str) -> bool:
        self._decode_neon_tx_return(tx)
        self._decode_neon_tx_event_list(tx)
        self._decode_neon_tx_from_holder(tx)

        if tx.neon_tx_res.is_valid() and (tx.status != NeonIndexedTxInfo.Status.Done):
            return self._decoding_done(tx, msg)

        return self._decoding_success(tx, msg)


class CreateAccount3IxDecoder(DummyIxDecoder):
    _name = 'CreateAccount3'
    _ix_code = 0x28
    _is_deprecated = False

    def execute(self) -> bool:
        ix = self.state.sol_neon_ix
        if len(ix.ix_data) < 20:
            return self._decoding_skip(f'not enough data to get Neon account {len(ix.ix_data)}')

        neon_account = "0x" + ix.ix_data[1:][:20].hex()
        pda_account = ix.get_account(2)

        account_info = NeonAccountInfo(
            neon_account, pda_account,
            ix.block_slot, None, ix.sol_sig
        )
        self.state.neon_block.add_neon_account(account_info, ix)
        return self._decoding_success(account_info, 'create Neon account')


class BaseTxIxDecoder(DummyIxDecoder, abc.ABC):
    _tx_type = NeonIndexedTxInfo.Type.Unknown

    def _get_neon_tx(self) -> Optional[NeonIndexedTxInfo]:
        ix = self.state.sol_neon_ix

        neon_tx_sig = ix.neon_tx_sig
        if len(neon_tx_sig) == 0:
            self._decoding_skip('no Neon tx hash in logs')
            return None

        block = self.state.neon_block
        key = NeonIndexedTxInfo.Key(ix)
        tx: Optional[NeonIndexedTxInfo] = block.find_neon_tx(key, ix)
        if tx is not None:
            return tx

        neon_tx = NeonTxInfo.from_neon_sig(neon_tx_sig)
        holder_account = self._get_holder_account()
        iter_blocked_account = self._iter_blocked_account()
        return block.add_neon_tx(self._tx_type, key, neon_tx, holder_account, iter_blocked_account, ix)

    @abc.abstractmethod
    def _get_holder_account(self) -> str:
        pass

    @abc.abstractmethod
    def _iter_blocked_account(self) -> Iterator[str]:
        pass


class BaseTxSimpleIxDecoder(BaseTxIxDecoder, abc.ABC):
    def _iter_blocked_account(self) -> Iterator[str]:
        return iter(())

    def _decode_neon_tx_lost_return(self, tx: NeonIndexedTxInfo) -> bool:
        ix = self.state.sol_neon_ix
        if not ix.is_log_truncated:
            return False

        tx.neon_tx_res.set_lost_result(ix.neon_total_gas_used)
        return True

    def _decode_neon_tx_cancel_return(self, tx: NeonIndexedTxInfo) -> bool:
        return False


class TxExecFromDataIxDecoder(BaseTxSimpleIxDecoder):
    _name = 'TransactionExecuteFromInstruction'
    _ix_code = 0x1f
    _is_deprecated = False
    _tx_type = NeonIndexedTxInfo.Type.Single

    def execute(self) -> bool:
        ix = self.state.sol_neon_ix
        if len(ix.ix_data) < 6:
            return self._decoding_skip('no enough data to get Neon tx')

        # 1 byte  - ix
        # 4 bytes - treasury index

        rlp_sig_data = ix.ix_data[5:]
        neon_tx = NeonTxInfo.from_sig_data(rlp_sig_data)
        if neon_tx.error:
            return self._decoding_skip(f'Neon tx rlp error: "{neon_tx.error}"')

        neon_tx_sig = ix.neon_tx_sig
        if neon_tx_sig != neon_tx.sig:
            return self._decoding_skip(f'Neon tx hash {neon_tx.sig} != {neon_tx_sig}')

        tx: Optional[NeonIndexedTxInfo] = self._get_neon_tx()
        if tx is None:
            return False
        tx.set_neon_tx(neon_tx, holder=None)
        return self._decode_tx(tx, 'Neon tx exec from data')

    def _get_holder_account(self) -> str:
        return ''


class TxExecFromAccountIxDecoder(BaseTxSimpleIxDecoder):
    _name = 'TransactionExecFromAccount'
    _ix_code = 0x2a
    _is_deprecated = False
    _tx_type = NeonIndexedTxInfo.Type.SingleFromAccount

    def execute(self) -> bool:
        ix = self.state.sol_neon_ix
        if len(ix.ix_data) < 5:
            return self._decoding_skip('no enough data for ix data')

        # 1 byte  - ix
        # 4 bytes - treasury index

        if ix.account_cnt < 1:
            return self._decoding_skip('no enough accounts to get holder account')

        tx: Optional[NeonIndexedTxInfo] = self._get_neon_tx()
        if tx is None:
            return False

        return self._decode_tx(tx, 'Neon tx exec from account')

    def _get_holder_account(self) -> str:
        return self.state.sol_neon_ix.get_account(0)


class BaseTxStepIxDecoder(BaseTxIxDecoder):
    _first_blocked_account_idx = 6

    def _get_neon_tx(self) -> Optional[NeonIndexedTxInfo]:
        ix = self.state.sol_neon_ix

        if ix.account_cnt < self._first_blocked_account_idx + 1:
            self._decoding_skip('no enough accounts')
            return None

        # 1 byte  - ix
        # 4 bytes - treasury index
        # 4 bytes - neon step cnt
        # 4 bytes - unique index

        if len(ix.ix_data) < 9:
            self._decoding_skip('no enough data to get Neon step cnt')
            return None

        neon_step_cnt = int.from_bytes(ix.ix_data[5:9], 'little')
        ix.set_neon_step_cnt(neon_step_cnt)

        return super()._get_neon_tx()

    def _get_holder_account(self) -> str:
        return self.state.sol_neon_ix.get_account(0)

    def _iter_blocked_account(self) -> Iterator[str]:
        return self.state.sol_neon_ix.iter_account(self._first_blocked_account_idx)

    def _decode_neon_tx_lost_return(self, tx: NeonIndexedTxInfo) -> bool:
        return False

    def decode_failed_neon_tx_event_list(self) -> None:
        ix = self.state.sol_neon_ix
        block = self.state.neon_block
        key = NeonIndexedTxInfo.Key(self.state.sol_neon_ix)
        tx: Optional[NeonIndexedTxInfo] = block.find_neon_tx(key, ix)
        if tx is None:
            return

        for event in self.state.sol_neon_ix.neon_tx_event_list:
            tx.add_neon_event(dataclasses.replace(
                event,
                total_gas_used=tx.len_neon_event_list,
                is_reverted=True,
                is_hidden=True,
                sol_sig=self.state.sol_neon_ix.sol_sig,
                idx=self.state.sol_neon_ix.idx,
                inner_idx=self.state.sol_neon_ix.inner_idx
            ))


class TxStepFromDataIxDecoder(BaseTxStepIxDecoder):
    _name = 'TransactionStepFromInstruction'
    _ix_code = 0x20
    _is_deprecated = False
    _tx_type = NeonIndexedTxInfo.Type.IterFromData

    def execute(self) -> bool:
        tx = self._get_neon_tx()
        if tx is None:
            return False

        if tx.neon_tx.is_valid():
            return self._decode_tx(tx, 'Neon tx continue step from data')

        ix = self.state.sol_neon_ix
        if len(ix.ix_data) < 14:
            return self._decoding_skip('no enough data to get Neon tx')

        # 1 byte  - ix
        # 4 bytes - treasury index
        # 4 bytes - neon step cnt
        # 4 bytes - unique index

        rlp_sig_data = ix.ix_data[13:]
        neon_tx = NeonTxInfo.from_sig_data(rlp_sig_data)
        if neon_tx.error:
            return self._decoding_skip(f'Neon tx rlp error "{neon_tx.error}"')

        if neon_tx.sig != tx.neon_tx.sig:
            return self._decoding_skip(f'Neon tx hash {neon_tx.sig} != tx log hash {tx.neon_tx.sig}')
        tx.set_neon_tx(neon_tx, holder=None)
        return self._decode_tx(tx, 'Neon tx init step from data')


class TxStepFromAccountIxDecoder(BaseTxStepIxDecoder):
    _name = 'TransactionStepFromAccount'
    _ix_code = 0x21
    _is_deprecated = False
    _tx_type = NeonIndexedTxInfo.Type.IterFromAccount

    def execute(self) -> bool:
        tx = self._get_neon_tx()
        if tx is None:
            return False

        return self._decode_tx(tx, 'Neon tx step from account')


class TxStepFromAccountNoChainIdIxDecoder(BaseTxStepIxDecoder):
    _name = 'TransactionStepFromAccountNoChainId'
    _ix_code = 0x22
    _is_deprecated = False
    _tx_type = NeonIndexedTxInfo.Type.IterFromAccountWoChainId

    def execute(self) -> bool:
        tx = self._get_neon_tx()
        if tx is None:
            return False

        return self._decode_tx(tx, 'Neon tx wo chain-id step from account')


class CollectTreasureIxDecoder(DummyIxDecoder):
    _name = 'CollectTreasure'
    _ix_code = 0x1e
    _is_deprecated = False


class CancelWithHashIxDecoder(DummyIxDecoder):
    _name = 'CancelWithHash'
    _ix_code = 0x23
    _is_deprecated = False
    _first_blocked_account_idx = 3

    def execute(self) -> bool:
        ix = self.state.sol_neon_ix
        if ix.account_cnt < self._first_blocked_account_idx + 1:
            return self._decoding_skip('no enough accounts')

        # 1 byte   - ix
        # 32 bytes - tx hash
        if len(ix.ix_data) < 33:
            return self._decoding_skip(f'no enough data to get Neon tx hash {len(ix.ix_data)}')

        neon_tx_sig: str = '0x' + ix.ix_data[1:33].hex().lower()
        log_tx_sig = ix.neon_tx_sig
        if log_tx_sig != neon_tx_sig:
            return self._decoding_skip(f'Neon tx hash "{log_tx_sig}" != "{neon_tx_sig}"')

        key = NeonIndexedTxInfo.Key(ix)
        tx = self.state.neon_block.find_neon_tx(key, ix)
        if not tx:
            return self._decoding_skip(f'cannot find Neon tx {neon_tx_sig}')

        tx.set_canceled(True)

        return self._decode_tx(tx, 'cancel Neon tx')


class CreateHolderAccountIx(DummyIxDecoder):
    _name = 'CreateHolderAccount'
    _ix_code = 0x24
    _is_deprecated = False


class DeleteHolderAccountIx(DummyIxDecoder):
    _name = 'DeleteHolderAccount'
    _ix_code = 0x25
    _is_deprecated = False


class WriteHolderAccountIx(DummyIxDecoder):
    _name = 'WriteHolderAccount'
    _ix_code = 0x26
    _is_deprecated = False

    def execute(self) -> bool:
        ix = self.state.sol_neon_ix
        if ix.account_cnt < 1:
            return self._decoding_skip(f'no enough accounts {ix.account_cnt}')

        # 1  byte  - ix
        # 32 bytes - tx hash
        # 8  bytes - offset

        # No enough bytes to get length of chunk
        if len(ix.ix_data) < 42:
            return self._decoding_skip(f'no enough data to get Neon tx data chunk {len(ix.ix_data)}')

        data = ix.ix_data[41:]
        chunk = NeonIndexedHolderInfo.DataChunk(
            offset=int.from_bytes(ix.ix_data[33:41], 'little'),
            length=len(data),
            data=data,
        )

        neon_tx_sig: str = '0x' + ix.ix_data[1:33].hex().lower()
        tx_sig = ix.neon_tx_sig
        if tx_sig != neon_tx_sig:
            return self._decoding_skip(f'Neon tx hash "{neon_tx_sig}" != log tx hash "{tx_sig}"')

        block = self.state.neon_block
        holder_account = ix.get_account(0)

        key = NeonIndexedTxInfo.Key(ix)
        tx: Optional[NeonIndexedTxInfo] = block.find_neon_tx(key, ix)
        if (tx is not None) and tx.neon_tx.is_valid():
            return self._decoding_success(tx, f'add surplus data chunk to tx')

        key = NeonIndexedHolderInfo.Key(holder_account, neon_tx_sig)
        holder: NeonIndexedHolderInfo = block.find_neon_tx_holder(key, ix) or block.add_neon_tx_holder(key, ix)

        # Write the received chunk into the holder account buffer
        holder.add_data_chunk(chunk)
        self._decoding_success(holder, f'add Neon tx data chunk {chunk}')

        if tx is not None:
            self._decode_neon_tx_from_holder(tx)

        return True


class Deposit3IxDecoder(DummyIxDecoder):
    _name = 'Deposit3'
    _ix_code = 0x27
    _is_deprecated = False


def get_neon_ix_decoder_list() -> List[Type[DummyIxDecoder]]:
    ix_decoder_list = [
        CreateAccount3IxDecoder,
        CollectTreasureIxDecoder,
        TxExecFromDataIxDecoder,
        TxExecFromAccountIxDecoder,
        TxStepFromDataIxDecoder,
        TxStepFromAccountIxDecoder,
        TxStepFromAccountNoChainIdIxDecoder,
        CancelWithHashIxDecoder,
        CreateHolderAccountIx,
        DeleteHolderAccountIx,
        WriteHolderAccountIx,
        Deposit3IxDecoder,
    ]

    for IxDecoder in ix_decoder_list:
        assert not IxDecoder.is_deprecated(), f"{IxDecoder.name()} is deprecated!"

    return ix_decoder_list
