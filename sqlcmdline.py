# !/usr/bin/env python3
"""Usage: sqlcmdline.py [-h | --help] -S <server> -d <database>
                                      (-E | -U <user> -P <password>)

Small command line utility to query MSSQL databases. The parameters are named
to match the official tool, "sqlcmd".

Arguments:
  -S <server>       Server name.
  -d <database>     Database to open.
  -E                Use Integrated Security.
  -U <user>         SQL Login user
  -P <password>     SQL Login password
"""
from docopt import docopt
import traceback
import pyodbc
import math
import os
import sys
import struct
import operator as op
from collections import defaultdict, namedtuple
from datetime import datetime, date
import decimal  # added for PyInstaller

PreparedCommand = namedtuple("PrepCmd", "query error callback")
ConnParams = namedtuple("ConnParams", "server database user password")

max_column_width = 100
max_rows_print = 50
chars_to_cleanup = str.maketrans("\n\t\r", "   ")

connection = None
conninfo = None


def command_help(modifiers, params):
    t = ('--Available commands--\n'
         'Syntax: :command required_parameter [optional_parameter].\n\n'
         'Common command modifiers are:\n'
         '\t-eq: makes the search text  parameter an exact match, by default'
         ' all parameters use LIKE comparisons\n'
         '\t-full: in some commands, will return * from '
         'INFORMATION_SCHEMA instead of a smaller subset of columns\n')
    print(t)
    sep = " -- "
    t = (f':help{sep}prints the command list\n'
         f':truncate [chars]{sep}truncates the results to columns of '
         f'maximum "chars" length. Default = 100. Setting to 0 shows full '
         f'contents.\n'
         f':rows [rownum]{sep}prints only "rownum" out of the whole resultset.'
         f' Default = 100. Setting to 0 prints all the rows.\n'
         f':tables [table_name]{sep}List all tables, or tables "like '
         f'table_name"\n'
         f':cols [-eq] table_name [-full]{sep}List columns for the table '
         f'"like table_name" (optionally, equal table_name).\n'
         f':views [view_name] [-full]{sep}List all views, or views "like '
         f'view_name"\n'
         f':procs [proc_name] [-full]{sep}List all procedures, or procs '
         f'"like proc_name"\n'
         f':funcs [func_name] [-full]{sep}List all functions, or functions '
         f'"like func_name"\n'
         f':src obj.name{sep}Will call "sp_helptext obj.name". Results won\'t'
         f' be truncated.\n'
         f':deps [to|from] obj.name{sep}Show dependencies to/from obj.name.\n'
         f':file path{sep}Opens a file and runs the script. No checking/'
         f'parsing of the file will take place.\n'
         f':dbs database_name{sep}List all databases, or databases "like '
         f'database_name".\n'
         f':use database_name{sep}changes the connection to "database_name".\n'
         f':timeout [seconds]{sep}sets the command timeout. '
         f'Default: 30 seconds.'
         f'\n')
    print(t)
    return (None, None, None)


def command_truncate(modifiers, params):
    try:
        global max_column_width
        if not params:
            print(f'Current ":truncate" value: {max_column_width}')
        else:
            col_size = int(params[0])
            if col_size < 0:
                raise
            max_column_width = col_size
            print("Truncate value set")
        return PreparedCommand(None, None, None)
    except Exception as e:
        return PreparedCommand(None, "Invalid arguments", None)


def command_rows(modifiers, params):
    try:
        global max_rows_print
        if not params:
            print(f'Current ":rows" value: {max_rows_print}')
        else:
            max_rows = int(params[0])
            if max_rows < 0:
                raise
            max_rows_print = max_rows
            msg = "ALL" if not max_rows_print else max_rows_print
            print(f"Printing set to {msg} rows of each resultset")
        return PreparedCommand(None, None, None)
    except Exception as e:
        return PreparedCommand(None, "Invalid arguments", None)


def command_tables(modifiers, params):
    q = f"SELECT * FROM INFORMATION_SCHEMA.TABLES "
    if params:
        if len(params) == 1:
            q += f"WHERE TABLE_NAME LIKE '%{params[0]}%'"
        else:
            return PreparedCommand(None, "Invalid arguments", None)
    return PreparedCommand(q, None, None)


def command_columns(modifiers, params):
    try:
        cols = ("*" if "-full" in modifiers else
                "TABLE_CATALOG, TABLE_SCHEMA, TABLE_NAME, "
                "COLUMN_NAME, DATA_TYPE")
        q = f"SELECT {cols} FROM INFORMATION_SCHEMA.COLUMNS "
        if "-eq" in modifiers:
            q += f"WHERE TABLE_NAME = '{params[0]}'"
        else:
            q += f"WHERE TABLE_NAME LIKE '%{params[0]}%'"
        return PreparedCommand(q, None, None)
    except IndexError as ie:
        return PreparedCommand(None, "Invalid arguments", None)
    except Exception as e:
        return PreparedCommand(None, str(e), None)


def command_views(modifiers, params):
    try:
        cols = ("*" if "-full" in modifiers else
                "TABLE_CATALOG, TABLE_SCHEMA, TABLE_NAME,"
                " CHECK_OPTION, IS_UPDATABLE")
        q = f"SELECT {cols} FROM INFORMATION_SCHEMA.VIEWS "
        if params:
            q += f"WHERE TABLE_NAME LIKE '%{params[0]}%'"
        return PreparedCommand(q, None, None)
    except Exception as e:
        return PreparedCommand(None, "Invalid arguments")


def command_procedures(modifiers, params):
    try:
        cols = ("*" if "-full" in modifiers else
                "ROUTINE_CATALOG, ROUTINE_SCHEMA, ROUTINE_NAME, "
                "DATA_TYPE, CREATED, LAST_ALTERED")
        q = (f"SELECT {cols} FROM INFORMATION_SCHEMA.ROUTINES WHERE "
             f"ROUTINE_TYPE = 'PROCEDURE' ")
        if params:
            q += f"AND ROUTINE_NAME LIKE '%{params[0]}%'"
        return PreparedCommand(q, None, None)
    except Exception as e:
        return PreparedCommand(None, "Invalid arguments", None)


def command_functions(modifiers, params):
    try:
        cols = ("*" if "-full" in modifiers else
                "ROUTINE_CATALOG, ROUTINE_SCHEMA, ROUTINE_NAME, "
                "DATA_TYPE, CREATED, LAST_ALTERED")
        q = (f"SELECT {cols} FROM INFORMATION_SCHEMA.ROUTINES WHERE "
             f"ROUTINE_TYPE = 'FUNCTION' ")
        if params:
            q += f"AND ROUTINE_NAME LIKE '%{params[0]}%'"
        return PreparedCommand(q, None, None)
    except Exception as e:
        return PreparedCommand(None, "Invalid arguments", None)


def command_source(modifiers, params):
    try:
        global max_column_width
        global max_rows_print
        current_trunc = max_column_width
        current_rows = max_rows_print

        def revert_truncate():
            global max_column_width
            global max_rows_print
            max_column_width = current_trunc
            max_rows_print = current_rows

        max_column_width = 0
        max_rows_print = 0
        q = f"sp_helptext '{params[0]}'"
        return PreparedCommand(q, None, revert_truncate)
    except Exception as e:
        return PreparedCommand(None, str(e), None)


def command_dependencies(modifiers, params):
    try:
        global max_column_width
        global max_rows_print
        current_trunc = max_column_width
        current_rows = max_rows_print

        def revert_truncate():
            global max_column_width
            global max_rows_print
            max_column_width = current_trunc
            max_rows_print = current_rows

        max_column_width = 0
        max_rows_print = 0
        if params[0] == 'from':
            # Depend on
            q = "EXEC sp_MSdependencies N'{name}', NULL, 1053183"
        elif params[0] == 'on':
            # Need me
            q = "EXEC sp_MSdependencies N'{name}', NULL, 1315327"
        else:
            return PreparedCommand(None, 'Invalid arguments', None)
        q = q.format(name=params[1])
        return PreparedCommand(q, None, revert_truncate)
    except Exception as e:
        return PreparedCommand(None, str(e), None)


def command_file(modifiers, params):
    global connection
    cursor = connection.cursor()
    try:
        # if the path had spaces it was space-split by
        # the process_command function
        path = " ".join(params)
        if path.startswith('"') and path.endswith('"'):
            # typical in "Copy as path" option from Explorer
            path = path[1:-1]
        command = []
        with open(path, 'r') as script:
            for line in script:
                if line.strip().upper().startswith('GO'):
                    # TODO: add logic to support GO [count]
                    cursor.execute(''.join(command))
                    rcount = cursor.rowcount  # -1 for "select" queries
                    if rcount == -1:
                        try:
                            output_results(cursor)
                        except pyodbc.ProgrammingError as pe:
                            # I should really filter for the specific message
                            # "No results.  Previous SQL was not a query."
                            print("Block executed, no rows returned or "
                                  "rowcount available")
                    else:
                        print("\nRows affected:", rcount, flush=True)
                    command = []
                else:
                    command.append(line)
        return PreparedCommand(None, None, None)
    except Exception as e:
        return PreparedCommand(None, str(e), None)


def command_databases(modifiers, params):
    q = f"SELECT name as 'Database Name' FROM master.dbo.sysdatabases "
    if params:
        if len(params) == 1:
            q += f"WHERE name LIKE '%{params[0]}%'"
        else:
            return PreparedCommand(None, "Invalid arguments", None)
    return PreparedCommand(q, None, None)


def command_use(modifiers, params):
    global conninfo
    message = None
    if params and len(params) == 1:
        old_conn = conninfo
        conninfo = ConnParams(conninfo.server, params[0], conninfo.user,
                              conninfo.password)
        try:
            create_connection()
        except:
            conninfo = old_conn
            message = f"Connection to database {params[0]} failed."
    else:
        message = "Invalid arguments"
    return PreparedCommand(None, message, None)


def command_timeout(modifiers, params):
    try:
        global connection
        if not params:
            print(f'Current ":timeout" value: {connection.timeout}')
        else:
            timeout = int(params[0])
            if timeout < 0:
                raise
            connection.timeout = timeout
            print(f"Command timeout set to {timeout} seconds.")
        return PreparedCommand(None, None, None)
    except Exception as e:
        return PreparedCommand(None, "Invalid arguments", None)


commands = {":help": command_help,
            ":tables": command_tables,
            ":cols": command_columns,
            ":views": command_views,
            ":procs": command_procedures,
            ":funcs": command_functions,
            ":truncate": command_truncate,
            ":rows": command_rows,
            ":src": command_source,
            ":deps": command_dependencies,
            ":file": command_file,
            ":dbs": command_databases,
            ":use": command_use,
            ":timeout": command_timeout}

custom_commands = {}


def text_formatter(value):
    value = str(value)
    value = str.translate(value, chars_to_cleanup)
    if max_column_width and len(value) > max_column_width:
        value = value[:max_column_width-5] + "[...]"
    return value


def output_results(cursor):
    try:
        print_resultset(cursor)
    except pyodbc.ProgrammingError as e:
        if "Previous SQL was not a query." in str(e):
            pass
        else:
            raise e
    while cursor.nextset():
        try:
            print_resultset(cursor)
        except pyodbc.ProgrammingError as e:
            if "Previous SQL was not a query." in str(e):
                continue
            else:
                raise e


def print_resultset(cursor):
    global max_rows_print
    if max_rows_print:
        odbc_rows = cursor.fetchmany(max_rows_print)
    else:
        odbc_rows = cursor.fetchall()
    column_names = [text_formatter(column[0]) for column in cursor.description]
    format_str, print_ready = format_rows(column_names, odbc_rows)
    print()  # blank line
    # Issue #3, printing too slow. Trade off memory for speed when printing
    # a resultset
    print("\n".join(format_str.format(*row) for row in print_ready),
          flush=True)
    # Turns out cursor.rowcount is not reliable. Ideally I woud like to
    # display the number of rows affected and how many printed. Since I can't
    # I'll settle for this alternative:
    printed_rows = len(odbc_rows)
    if printed_rows < max_rows_print or max_rows_print == 0:
        print(f"\nRows returned: {printed_rows}\n")
    else:
        rowcount = "(unknown)" if cursor.rowcount == -1 else cursor.rowcount
        print(f"\nRows printed: {max_rows_print}. Total rows: {rowcount}\n",
              flush=True)


def format_rows(column_names, raw_rows):
    # lenghts will match columns by position
    column_widths = defaultdict(lambda: 0)
    formatted = []
    for row in raw_rows:
        new_row = []
        for index, value in enumerate(row):
            new_value = "#unknown#"
            if value is None:
                new_len = 6
                new_value = "[NULL]"
            if isinstance(value, bool):
                new_len = 6
                new_value = value
            elif isinstance(value, datetime):
                new_len = 26
                new_value = value.isoformat()
            # order matters, datetime matches date :)
            elif isinstance(value, date):
                new_len = 10
                new_value = value.isoformat()
            elif isinstance(value, int):
                new_len = int_len(value)
                new_value = value
            elif isinstance(value, (float, decimal.Decimal)):
                new_len = decimal_len(decimal.Decimal(value))
                new_value = value
            elif isinstance(value, str):
                new_value = text_formatter(value)
                new_len = len(new_value)
            if new_len > column_widths[index]:
                column_widths[index] = new_len
            new_row.append(new_value)
        formatted.append(tuple(new_row))

    for index, col_name in enumerate(column_names):
        column_widths[index] = max((column_widths[index], len(col_name)))

    format_str = "|".join(["{{{ndx}:{len}}}".format(ndx=ndx, len=len)
                           for ndx, len in column_widths.items()])
    formatted.insert(0, column_names)
    # IIRC now dicts are ordered but just in case/for other implementations
    formatted.insert(1, ["-"*width for index, width in
                         sorted(column_widths.items(),
                                key=op.itemgetter(0))])
    return format_str, formatted


def int_len(number):
    # Source:
    # http://stackoverflow.com/questions/2189800/length-of-an-integer-in-python
    if number > 0:
        digits = int(math.log10(number))+1
    elif number == 0:
        digits = 1
    else:
        digits = int(math.log10(-number))+2  # +1 if you don't count the '-'
    return digits


def decimal_len(decimal_number):
    sign, digits, _ = decimal_number.as_tuple()
    # digits + separator + sign (where sign is either 0 or 1 for negatives)
    return len(digits) + 1 + sign


def process_command(line_typed):
    try:
        command_name, *rest = line_typed.split(" ")
    except:
        command_name = "Nope"
    modifiers = [x for x in rest if x.startswith("-")]
    params = [x for x in rest if x not in modifiers]
    if command_name in commands:
        command_handler = commands[command_name]
        query, error, cb = command_handler(modifiers, params)
    elif command_name in custom_commands:
        template = custom_commands[command_name]
        query = template.format(*params)
        error = None
        cb = None
    else:
        t = "Invalid command name. Use :help for a list of available commands."
        return None, t, None
    if not error and query:
        print(f"Query: {query}")
    return query, error, cb


def determine_directory():
    # for compatibility with pyinstaller
    if getattr(sys, 'frozen', False):
        # we are running in a bundle
        _dir = sys._MEIPASS
    else:
        # we are running in a normal Python environment
        _dir = os.path.dirname(os.path.abspath(__file__))
    return _dir


def load_custom_commands():
    _dir = determine_directory()
    comm_file = os.path.join(_dir, "commands.scl")
    if not os.path.isfile(comm_file):
        return  # no error or anything
    with open(comm_file) as f:
        for line in f:
            c, q = line.split(' ', 1)
            custom_commands[c] = q


# source:
# https://github.com/mkleehammer/pyodbc/wiki/Using-an-Output-Converter-function
def handle_datetimeoffset(dto_value):
    # see also:
    # https://github.com/mkleehammer/pyodbc/issues/134#issuecomment-281739794
    tup = struct.unpack("<6hI2h", dto_value)
    tweaked = [tup[i] // 100 if i == 6 else tup[i] for i in range(len(tup))]
    t = "{:04d}-{:02d}-{:02d} {:02d}:{:02d}:{:02d}.{:07d} {:+03d}:{:02d}"
    return t.format(*tweaked)


def create_connection():
    global connection
    global conninfo
    connection = (f"Driver={{SQL Server Native Client 11.0}};"
                  f"Server={conninfo.server};"
                  f"Database={conninfo.database};")
    if not conninfo.user:
        connection += "Trusted_Connection=Yes;"
    else:
        connection += f"Uid={conninfo.user};Pwd={conninfo.password};"
    conn = pyodbc.connect(connection, autocommit=True)
    conn.add_output_converter(-155, handle_datetimeoffset)
    conn.timeout = 30
    connection = conn


def prompt_query_command():
    lines = []
    while True:
        lines.append(input(">"))
        last = lines[-1]
        if last.strip()[-2:] == ";;":
            return '\n'.join(lines)[:-1]  # Exclude extra ";"
        if last.strip().upper().startswith('GO'):
            return '\n'.join(lines[:-1])  # Exclude GO
        if last.startswith(":"):
            return lines[-1]  # for commands ignore previous lines


def query_loop():
    global connection
    global conninfo
    print(f'Connected to server {conninfo.server} '
          f'database {conninfo.database}')
    print()
    print('Special commands are prefixed with ":". For example, use ":exit" '
          'or ":quit" to finish your session. Everything else is sent '
          'directly to the server using ODBC.')
    print('Use ":help" to get a list of commands available')
    print(f"{conninfo.server}@{conninfo.database}")
    query = prompt_query_command()
    callback = None
    while query not in (":exit", ":quit"):
        try:
            print()  # blank line
            if query.startswith(":"):
                query, cmd_error, callback = process_command(query)
                if cmd_error:
                    print(f"Command error: {cmd_error}")
            if query:
                # print("\n----------\n", query, "\n----------\n")
                cursor = connection.cursor()
                cursor.execute(query)
                rcount = cursor.rowcount  # -1 for "select" queries
                if rcount == -1:
                    output_results(cursor)
                else:
                    print("\nRows affected:", rcount, flush=True)
                if callback:
                    callback()
                    callback = None
        except Exception as e:
            print("---ERROR---\n", flush=True)
            traceback.print_exc()
            print("\n---ERROR---")
        print(flush=True)  # blank line
        print(f"{conninfo.server}@{conninfo.database}")
        query = prompt_query_command()


if __name__ == "__main__":
    arguments = docopt(__doc__)
    server = arguments["-S"]
    database = arguments["-d"]
    user = arguments["-U"]
    password = arguments["-P"]
    conninfo = ConnParams(server, database, user, password)
    load_custom_commands()
    create_connection()
    query_loop()
