#!/usr/bin/env python3
"""Infer a configuration automatically from a Beancount ledger.
"""

__copyright__ = "Copyright (C) 2020  Martin Blais"
__license__ = "GNU GPLv2"

from typing import List, Optional, Tuple, Set
import argparse
import collections
import datetime
import sys
import logging
import re

from beancount import loader
from beancount.core import account as accountlib
from beancount.core import account_types as acctypes
from beancount.core import data
from beancount.core import getters
from beancount.parser import options

from beangrow.config_pb2 import Config
from beangrow.config_pb2 import InvestmentConfig
from beangrow.config_pb2 import GroupConfig


# Basic type aliases.
Account = str
Currency = str
Date = datetime.date


def find_investments(entries: data.Entries,
                     options_map: data.Options,
                     start_date: Optional[Date]) -> List[Tuple[Account, Currency, Optional[str]]]:
    """Return a list of (account, currency, tag) found in transactions."""
    atypes = options.get_account_types(options_map)
    operating_currencies = set(options_map.get('operating_currency', []))
    commodities = set(getters.get_commodity_directives(entries))
    
    investments = set()
    for entry in data.filter_txns(entries):
        if start_date and entry.date < start_date:
            continue
        for posting in entry.postings:
            if not acctypes.is_balance_sheet_account(posting.account, atypes):
                continue
            if posting.units and posting.units.currency in commodities and \
               posting.units.currency not in operating_currencies:
                tag = sorted(list(entry.tags))[0] if entry.tags else None
                investments.add((posting.account, posting.units.currency, tag))
                
    return sorted(list(investments), key=lambda x: (x[0], x[1], x[2] or ""))


def infer_configuration(entries: data.Entries,
                        options_map: data.Options,
                        start_date: Optional[Date]) -> Config:
    """Infer an input configuration from a ledger's contents."""

    # Find out the list of (account, currency, tag) triplets.
    investment_list = find_investments(entries, options_map, start_date)

    # Figure out the available investments.
    config = Config()
    infer_investments_configuration(entries, investment_list, config.investments)

    # Create reasonable reporting groups.
    operating_currencies = options_map.get('operating_currency', [])
    infer_report_groups(entries, config.investments, config.groups, operating_currencies)
    return config


def infer_investments_configuration(entries: data.Entries,
                                    investment_list: List[Tuple[Account, Currency, Optional[str]]],
                                    out_config: InvestmentConfig):
    """Infer a reasonable configuration for input."""

    all_accounts = set(getters.get_account_open_close(entries))

    for account, currency, tag in investment_list:
        aconfig = out_config.investment.add()
        aconfig.currency = currency
        aconfig.asset_account = f"{account}#{tag}" if tag else account

        # Pattern matching for dividends (handled for consolidated structure)
        # Match accounts containing the base account path and the currency
        account_base = account.replace("Assets:", "")
        regexp = re.compile(rf".*:{account_base}.*:{currency}:Dividends?")
        for maccount in filter(regexp.match, all_accounts):
            aconfig.dividend_accounts.append(maccount)

        match_accounts = set()
        match_accounts.add(account) # Match base account
        match_accounts.update(aconfig.dividend_accounts)
        match_accounts.update(aconfig.match_accounts)

        # Figure out the total set of accounts seen in those transactions.
        cash_accounts = set()
        for entry in data.filter_txns(entries):
            if any(posting.account in match_accounts for posting in entry.postings):
                for posting in entry.postings:
                    if (posting.account == account or
                        posting.account in aconfig.dividend_accounts or
                        posting.account in aconfig.match_accounts):
                        continue
                    if (re.search(r":(Cash|Checking|Receivable|GSURefund)$",
                                  posting.account) or
                        re.search(r"Receivable|Payable", posting.account) or
                        re.match(r"Income:.*:(Match401k)$", posting.account)):
                        cash_accounts.add(posting.account)
        aconfig.cash_accounts.extend(cash_accounts)


def infer_report_groups(entries: data.Entries,
                        investments: InvestmentConfig,
                        out_config: GroupConfig,
                        operating_currencies: List[str]):
    """Logically group accounts for reporting."""
    groups = collections.defaultdict(list)

    # Create strategy groups for each tag discovered.
    for investment in investments.investment:
        if "#" in investment.asset_account:
            tag = investment.asset_account.split("#", 1)[1]
            name = f"strategy.{tag}"
            groups[name].append(investment.asset_account)

    for investment in investments.investment:
        base_account = investment.asset_account.split("#", 1)[0]
        name = f"account.{base_account.replace(':', '_')}"
        # If we include the base account, our 'Relaxed Isolation' patch 
        # already makes it include all tagged trades.
        groups[name].append(base_account)

    for name, group_accounts in sorted(groups.items()):
        report = out_config.group.add()
        report.name = name
        # Force unique accounts in the group to prevent double processing
        unique_accounts = []
        seen = set()
        for acc in group_accounts:
            if acc not in seen:
                unique_accounts.append(acc)
                seen.add(acc)
        report.investment.extend(unique_accounts)
        
        # Every group must specify a currency if it contains multi-cost investments.
        if operating_currencies:
            report.currency = operating_currencies[0]


def main():
    """Top-level function."""
    parser = argparse.ArgumentParser(description=__doc__.strip())

    parser.add_argument('ledger',
                        help="Beancount ledger file.")
    parser.add_argument('config', nargs='?', action='store',
                        help='Output configuration for accounts and reports.')

    parser.add_argument('-v', '--verbose', action='store_true',
                        help='Verbose mode.')

    parser.add_argument('-s', '--start-date', action='store',
                        type=datetime.date.fromisoformat,
                        default=None,
                        help=("Accounts already closed before this date will not be "
                              "included in reporting."))

    args = parser.parse_args()
    if args.verbose:
        logging.basicConfig(level=logging.DEBUG, format='%(levelname)-8s: %(message)s')
        logging.getLogger('matplotlib.font_manager').disabled = True

    # Load the example file.
    logging.info("Reading ledger: %s", args.ledger)
    entries, _, options_map = loader.load_file(args.ledger)

    # Infer configuration proto.
    logging.info("Inferring configuration.")
    config = infer_configuration(entries, options_map, args.start_date)

    logging.info("Done.")
    outfile = open(args.config, "w") if args.config else sys.stdout
    print(config, file=outfile)


if __name__ == '__main__':
    main()
