import time
import json
import copy
import logging
import functools
import aiometer
import anyio
import itertools
from dataclasses import dataclass, field

from transaction import Transaction
from contract import Pool
from contract.factory import Factory, create_factory
from querier import Querier
from swap import calculate_swap


@dataclass
class State:
    """@DEV TODO: Add more update all jobs if adding new strategy.
       currently works for dex / arb related strategies.
    """
    contracts: dict[str, Pool] = field(default_factory=dict)
    update_all_tokens_jobs = field(default_factory=list)
    update_all_reserves_jobs = field(default_factory=list)
    update_all_fees_jobs = field(default_factory=list)
        
    async def set_all_pool_contracts(self,
                                     querier: Querier,
                                     factory_contracts: dict,
                                     arb_denom: str):
        """ This function is used to set all the pool contracts
            in state taking into account factory contracts and
            contracts loaded into the bot.
        """
        for protocol in factory_contracts:
            factory: Factory = create_factory(
                                    contract_address=factory_contracts[protocol],
                                    protocol=protocol
                                    )
            all_pairs = await factory.get_all_pairs(querier=querier)
            self.contracts = {pair['contract_addr']: 
                                    Pool(contract_address=pair['contract_addr'], 
                                         protocol=protocol) 
                                    for pair in all_pairs}   
            
        self.set_all_jobs(querier=querier)
        await self.update_all(self.update_all_tokens_jobs)
        await self.update_all(self.update_all_fees_jobs)
        await self.update_all(self.update_all_reserves_jobs) 
        self.filter_out_zero_reserves()
        self.set_cyclic_routes(arb_denom=arb_denom)
            
    def set_all_jobs(self, querier: Querier):
        """ This function is used to set all the jobs"""
        self.update_all_tokens_jobs = [functools.partial(contract.update_tokens, querier) 
                                            for contract in self.contracts.values()]
        self.update_all_reserves_jobs = [functools.partial(contract.update_reserves, querier) 
                                            for contract in self.contracts.values()]
        self.update_all_fees_jobs = [functools.partial(contract.update_fees, querier) 
                                            for contract in self.contracts.values()]
        
    async def update_all(self, jobs: list) -> bool:
        """ This function is used to update all the contracts
            given a jobs list. It also organizes error handling.
        """
        try:
            await aiometer.run_all(jobs)
            return True
        except anyio._backends._asyncio.ExceptionGroup as e:
            logging.error(f"ExcetionGroup {e}: Sleeping for 60 seconds...")
            time.sleep(60)
            return False
        except json.decoder.JSONDecodeError as e:
            logging.error(f"JSON Exception {e}: Sleeping for 60 seconds...")
            time.sleep(60)
            return False
        except Exception as e:
            logging.error(f"General Exception {e}: Sleeping for 60 seconds...")
            time.sleep(60)
            return False
        
    def filter_out_zero_reserves(self):
        """ This function is used to filter out contracts
            that have 0 reserves.
        """
        self.contracts = {
            contract_address: contract
            for contract_address, contract in self.contracts.items()
            if (contract.token1_reserves > 0 and contract.token2_reserves > 0)
        }
        
    def set_cyclic_routes(self, arb_denom: str):
        """ This function is used to set the cyclic routes
            for the bot to use.
        """
        token_pairs = self._generate_token_pairs()
        self._set_contract_routes(arb_denom, token_pairs)
                                    
    def _generate_token_pairs(self) -> dict[str, dict[str, list]]:
        """ This function is used to generate a nested dictionary
            mapping token pairs to their respective contracts.
        """
        token_pairs: dict[str, dict[str, list]] = {}
        for contract_address, contract_info in self.contracts.items():
            denoms = [contract_info.token1_denom, contract_info.token2_denom]
            
            for denom in denoms:
                other_denom = denoms[0] if denom == denoms[1] else denoms[1]
                if denom not in token_pairs:
                    token_pairs[denom] = {other_denom: [contract_address]}
                    continue
                token_pairs[denom].setdefault(other_denom, []).append(contract_address) 
                
        return token_pairs
                        
    def _set_contract_routes(self, 
                             arb_denom: str, 
                             token_pairs: dict[str, dict[str, list]]):
        """ This function is used to set the routes for each contract."""
        set_routes = []
        for denom in token_pairs[arb_denom]:
            for denom_2 in token_pairs[denom]:
                if denom_2 not in token_pairs[arb_denom]:
                    continue
                contracts = itertools.product(
                                token_pairs[arb_denom][denom],
                                token_pairs[denom][denom_2],
                                token_pairs[denom_2][arb_denom],
                                )
                for contract_addresses in contracts:
                    route = list(contract_addresses)
                    set_route = set(route)
                    if set_route in set_routes:
                        continue
                    set_routes.append(set_route)
                    for contract_address in route:
                        self.contracts[contract_address].routes.append(route)
        
    def simulate_transaction(self, transaction: Transaction) -> dict[str, Pool]:
        """ Simulate a transaction on a copy of state and return the new state.
            This method does not modify the original state.
        """
        contracts: dict[str, Pool] = copy.deepcopy(self.contracts)
        
        for swap in transaction.swaps:
            pool = contracts[swap.contract_address]
            
            input_reserves, output_reserves = pool.get_reserves_from_input_denom(
                                                        swap.input_denom
                                                        )
            if swap.input_amount is None and amount_out is not None:
                swap.input_amount = amount_out

            amount_out, new_reserves_in, new_reserves_out = calculate_swap(
                            reserves_in=input_reserves,
                            reserves_out=output_reserves,
                            amount_in=swap.input_amount,
                            lp_fee=pool.lp_fee,
                            protocol_fee=pool.protocol_fee,
                            fee_from_input=pool.fee_from_input
                            )
            pool.input_reserves = new_reserves_in
            pool.output_reserves = new_reserves_out
            
        return contracts