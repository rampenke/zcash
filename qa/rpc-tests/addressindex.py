#!/usr/bin/env python
# Copyright (c) 2019 The Zcash developers
# Distributed under the MIT software license, see the accompanying
# file COPYING or http://www.opensource.org/licenses/mit-license.php.

#
# Test addressindex generation and fetching for insightexplorer
# 
# RPCs tested here:
#
#   getaddresstxids
#   getaddressbalance
#   getaddressdeltas
#   getaddressutxos
#   getaddressmempool
#

from test_framework.test_framework import BitcoinTestFramework

from test_framework.util import assert_equal
from test_framework.util import initialize_chain_clean
from test_framework.util import start_nodes, stop_nodes, connect_nodes
from test_framework.util import wait_bitcoinds

from test_framework.script import CScript, OP_HASH160, OP_EQUAL

from test_framework.mininode import COIN, CTransaction
from test_framework.mininode import CTxIn, CTxOut, COutPoint

from binascii import hexlify

class AddressIndexTest(BitcoinTestFramework):

    def setup_chain(self):
        print("Initializing test directory "+self.options.tmpdir)
        initialize_chain_clean(self.options.tmpdir, 3)

    def setup_network(self):
        # -insightexplorer causes addressindex to be enabled (fAddressIndex = true)

        self.nodes = start_nodes(3, self.options.tmpdir,
            [['-debug', '-txindex', '-experimentalfeatures', '-insightexplorer']]*3)
        connect_nodes(self.nodes[0], 1)
        connect_nodes(self.nodes[0], 2)

        self.is_network_split = False
        self.sync_all()

    def run_test(self):
        self.nodes[0].generate(105)
        self.sync_all()

        assert_equal(self.nodes[1].getblockcount(), 105)
        assert_equal(self.nodes[1].getbalance(), 0)

        # this list is only the first 5; subsequent are not yet mature
        unspent_txids = [ u['txid'] for u in self.nodes[0].listunspent() ]

        # Currently our only unspents are coinbase transactions, choose any one
        txid = unspent_txids[0]
        transaction = self.nodes[0].getrawtransaction(txid, 1)

        # It just so happens that the first output is the mining reward,
        # which has type pay-to-public-key-hash, and the second output
        # is the founders' reward, which has type pay-to-script-hash.
        addr_p2pkh = transaction['vout'][0]['scriptPubKey']['addresses'][0]
        addr_p2sh = transaction['vout'][1]['scriptPubKey']['addresses'][0]

        # Check that balances from mining are correct (105 blocks mined); in
        # regtest, all mining rewards are sent to the same pair of addresses.
        miners_expected = 105*10*COIN # units are zatoshis
        miners_actual = self.nodes[1].getaddressbalance(addr_p2pkh)
        assert_equal(miners_actual['balance'], miners_expected)
        assert_equal(miners_actual['received'], miners_expected)

        founders_expected = 105*2.5*COIN
        founders_actual = self.nodes[1].getaddressbalance(addr_p2sh)
        assert_equal(founders_actual['balance'], founders_expected)
        assert_equal(founders_actual['received'], founders_expected)

        # Multiple address arguments, results are the sum
        bal = self.nodes[1].getaddressbalance({
            'addresses': [addr_p2sh, addr_p2pkh]
        })
        total_expected = founders_expected + miners_expected
        assert_equal(bal['balance'], total_expected)
        assert_equal(bal['received'], total_expected)

        assert_equal(len(self.nodes[1].getaddresstxids(addr_p2pkh)), 105)
        assert_equal(len(self.nodes[1].getaddresstxids(addr_p2sh)), 105)

        # only the oldest 5 transactions are in the unspent list
        height_txids = self.nodes[1].getaddresstxids({
            'addresses': [addr_p2pkh, addr_p2pkh],
            'start': 1,
            'end': 5
        })
        assert_equal(sorted(height_txids), sorted(unspent_txids))

        height_txids = self.nodes[1].getaddresstxids({
            'addresses': [addr_p2sh],
            'start': 1,
            'end': 5
        })
        assert_equal(sorted(height_txids), sorted(unspent_txids))

        # each txid should appear only once
        height_txids = self.nodes[1].getaddresstxids({
            'addresses': [addr_p2pkh, addr_p2sh],
            'start': 1,
            'end': 5
        })
        assert_equal(sorted(height_txids), sorted(unspent_txids))

        # do some transfers, make sure balances are good
        txids_a = []
        a = self.nodes[1].getnewaddress()
        expected = 0
        expected_deltas = [] # for checking getaddressdeltas (below)
        for i in range(5):
            # first transaction happens at height 105, mined in block 106
            txid = self.nodes[0].sendtoaddress(a, i+1)
            txids_a.append(txid)
            self.nodes[0].generate(1)
            self.sync_all()
            expected += i+1
            expected_deltas.append({
                'height': 106+i,
                'satoshis': (i+1)*COIN,
                'txid': txid,
            })
        bal = self.nodes[1].getaddressbalance(a)
        assert_equal(bal['balance'], expected*COIN)
        assert_equal(bal['received'], expected*COIN)
        assert_equal(sorted(self.nodes[0].getaddresstxids(a)), sorted(txids_a))
        assert_equal(sorted(self.nodes[1].getaddresstxids(a)), sorted(txids_a))

        # Restart all nodes to ensure indices are saved to disk and recovered
        stop_nodes(self.nodes)
        wait_bitcoinds()
        self.setup_network()

        bal = self.nodes[1].getaddressbalance(a)
        assert_equal(bal['balance'], expected*COIN)
        assert_equal(bal['received'], expected*COIN)
        assert_equal(sorted(self.nodes[0].getaddresstxids(a)), sorted(txids_a))
        assert_equal(sorted(self.nodes[1].getaddresstxids(a)), sorted(txids_a))

        # Send 3 from address a, but -- subtlety alert! -- address a at this
        # time has 4 UTXOs, with values 1, 2, 3, 4. Sending value 3 requires
        # using up the value 4 UTXO, because of the tx fee
        # (the 3 UTXO isn't quite large enough).
        #
        # The txid from sending *from* address a is also added to the list of
        # txids associated with that address (test will verify below).

        b = self.nodes[2].getnewaddress()
        txid = self.nodes[1].sendtoaddress(b, 3)
        self.sync_all()

        # the one tx in the mempool refers to addresses a and b (dups ignored)
        mempool = self.nodes[0].getaddressmempool({'addresses': [b, a, b]})
        assert_equal(len(mempool), 3)
        assert_equal(mempool[1]['address'], a)
        assert_equal(mempool[1]['satoshis'], (-4)*COIN)
        assert_equal(mempool[1]['txid'], txid)
        for i in (0, 2):
            assert_equal(mempool[i]['address'], b)
            assert_equal(mempool[i]['satoshis'], 3*COIN)
            assert_equal(mempool[i]['txid'], txid)
        # a single address can be specified as a string (not json object)
        assert_equal([mempool[1]], self.nodes[0].getaddressmempool(a))

        txids_a.append(txid)
        expected_deltas.append({
            'height': 111,
            'satoshis': (-4)*COIN,
            'txid': txid,
        })
        self.sync_all() # ensure transaction is included in the next block
        self.nodes[0].generate(1)
        self.sync_all()

        # the send to b tx is now in a mined block, no longer in the mempool
        mempool = self.nodes[0].getaddressmempool({'addresses': [b, a]})
        assert_equal(len(mempool), 0)

        bal = self.nodes[2].getaddressbalance(a)
        assert_equal(bal['received'], expected*COIN)
        # the value 4 UTXO is no longer in our balance
        assert_equal(bal['balance'], (expected-4)*COIN)

        # Ensure the change from that transaction appears
        tx = self.nodes[0].getrawtransaction(txid, 1)
        change_vout = filter(lambda v: v['valueZat'] != 3*COIN, tx['vout'])
        change = change_vout[0]['scriptPubKey']['addresses'][0]
        bal = self.nodes[2].getaddressbalance(change)
        assert(bal['received'] > 0)
        assert(bal['received'] < (4-3)*COIN)
        assert_equal(bal['received'], bal['balance'])
        assert_equal(self.nodes[0].getaddresstxids(change), [txid])

        # Further checks that limiting by height works

        # non-overlapping range returns an empty list
        height_txids = self.nodes[1].getaddresstxids({
            'addresses': [a],
            'start': 106,
            'end': 105
        })
        assert_equal(height_txids, [])

        # various ranges
        for i in range(5):
            height_txids = self.nodes[1].getaddresstxids({
                'addresses': [a],
                'start': 106,
                'end': 106+i
            })
            assert_equal(height_txids, txids_a[0:i+1])

        height_txids = self.nodes[1].getaddresstxids({
            'addresses': [a],
            'start': 0,
            'end': 108
        })
        assert_equal(height_txids, txids_a[0:3])
        # end=0 means return the entire range
        height_txids = self.nodes[1].getaddresstxids({
            'addresses': [a],
            'start': 107,
            'end': 0
        })
        assert_equal(height_txids, txids_a)

        # Further check specifying multiple addresses
        txids_all = list(txids_a)
        txids_all += self.nodes[1].getaddresstxids(addr_p2pkh)
        txids_all += self.nodes[1].getaddresstxids(addr_p2sh)
        multitxids = self.nodes[1].getaddresstxids({
            'addresses': [a, addr_p2sh, addr_p2pkh]
        })
        # No dups in return list from getaddresstxids
        assert_equal(len(multitxids), len(set(multitxids)))

        # set(txids_all) removes its (expected) duplicates
        assert_equal(set(multitxids), set(txids_all))

        # Check that outputs with the same address in the same tx return one txid
        # (can't use createrawtransaction() as it combines duplicate addresses)
        addr = "t2LMJ6Arw9UWBMWvfUr2QLHM4Xd9w53FftS"
        addressHash = "97643ce74b188f4fb6bbbb285e067a969041caf2".decode('hex')
        scriptPubKey = CScript([OP_HASH160, addressHash, OP_EQUAL])
        unspent = filter(lambda u: u['amount'] >= 4, self.nodes[0].listunspent())
        tx = CTransaction()
        tx.vin = [CTxIn(COutPoint(int(unspent[0]['txid'], 16), unspent[0]['vout']))]
        tx.vout = [CTxOut(1*COIN, scriptPubKey), CTxOut(2*COIN, scriptPubKey)]
        tx = self.nodes[0].signrawtransaction(hexlify(tx.serialize()).decode('utf-8'))
        txid = self.nodes[0].sendrawtransaction(tx['hex'], True)
        self.nodes[0].generate(1)
        self.sync_all()

        assert_equal(self.nodes[1].getaddresstxids(addr), [txid])
        bal = self.nodes[2].getaddressbalance(addr)
        assert_equal(bal['balance'], 3*COIN)
        assert_equal(bal['received'], 3*COIN)

        deltas = self.nodes[1].getaddressdeltas({
            'addresses': [a],
        })
        assert_equal(len(deltas), len(expected_deltas))
        for z in zip(deltas, expected_deltas):
            assert_equal(z[0]['address'],   a)
            assert_equal(z[0]['height'],    z[1]['height'])
            assert_equal(z[0]['satoshis'],  z[1]['satoshis'])
            assert_equal(z[0]['txid'],      z[1]['txid'])

        deltas_limited = self.nodes[1].getaddressdeltas({
            'addresses': [a],
            'start': 106,   # the full range (also the default)
            'end': 111,
        })
        assert_equal(deltas_limited, deltas)

        deltas_limited = self.nodes[1].getaddressdeltas({
            'addresses': [a],
            'start': 107,   # only the first element missing
            'end': 211,     # okay if this is beyond chain height
        })
        assert_equal(deltas_limited, deltas[1:])

        deltas_limited = self.nodes[1].getaddressdeltas({
            'addresses': [a],
            'start': 109,   # only the fourth element
            'end': 109,
        })
        assert_equal(deltas_limited, deltas[3:4])

        deltas_info = self.nodes[1].getaddressdeltas({
            'addresses': [a],
            'start': 106,   # the full range (also the default)
            'end': 112,
            'chainInfo': True,
        })
        assert_equal(deltas_info['deltas'], deltas)

        # check the additional items returned by chainInfo
        assert_equal(deltas_info['start']['height'], 106)
        block_hash = self.nodes[1].getblockhash(106)
        assert_equal(deltas_info['start']['hash'], block_hash)

        assert_equal(deltas_info['end']['height'], 112)
        block_hash = self.nodes[1].getblockhash(112)
        assert_equal(deltas_info['end']['hash'], block_hash)

        # Test getaddressutxos by comparing results with deltas
        utxos = self.nodes[1].getaddressutxos(a)

        # The value 4 note was spent, so won't show up in the utxo list,
        # so for comparison, remove the 4 (and -4 for output) from the
        # deltas list
        deltas = self.nodes[1].getaddressdeltas({'addresses': [a]})
        deltas = filter(lambda d: abs(d['satoshis']) != 4*COIN, deltas)
        assert_equal(len(utxos), len(deltas))
        for z in zip(utxos, deltas):
            assert_equal(z[0]['address'],       a)
            assert_equal(z[0]['height'],        z[1]['height'])
            assert_equal(z[0]['satoshis'],      z[1]['satoshis'])
            assert_equal(z[0]['txid'],          z[1]['txid'])

if __name__ == '__main__':
    AddressIndexTest().main()
