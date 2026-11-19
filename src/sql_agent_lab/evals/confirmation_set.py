"""Held-out questions. They are not used to change retrieval or prompts.

Expected rows are the frozen result. No model is called from this module.
"""

CONFIRMATION_CASES = [
    {'id': 'C01', 'question': 'How many members?', 'answer_tables': ['members'], 'expected_rows': [[3]]},
    {'id': 'C02', 'question': 'How many books?', 'answer_tables': ['books'], 'expected_rows': [[3]]},
    {'id': 'C03', 'question': 'List book titles', 'answer_tables': ['books'], 'expected_rows': [['Dune'], ['Emma'], ['Kindred']]},
    {'id': 'C04', 'question': 'List member names', 'answer_tables': ['members'], 'expected_rows': [['Ann'], ['Bea'], ['Cal']]},
    {'id': 'C05', 'question': 'How many loans?', 'answer_tables': ['loans'], 'expected_rows': [[3]]},
    {'id': 'C06', 'question': 'member name for each loan', 'answer_tables': ['members', 'loans'], 'expected_rows': [['Ann'], ['Ann'], ['Bea']]},
    {'id': 'C07', 'question': 'book title for each loan', 'answer_tables': ['books', 'loans'], 'expected_rows': [['Dune'], ['Emma'], ['Dune']]},
    {'id': 'C08', 'question': 'How many open loans?', 'answer_tables': ['loans'], 'expected_rows': [[2]], 'tags': ['definition']},
    {'id': 'C09', 'question': "How many loans have status open?", 'answer_tables': ['loans'], 'expected_rows': [[2]], 'tags': ['no-definition']},
    {'id': 'C10', 'question': 'member names in alphabetical order', 'answer_tables': ['members'], 'expected_rows': [['Ann'], ['Bea'], ['Cal']], 'ordered': True},
    {'id': 'C11', 'question': 'How many members live in Kent?', 'answer_tables': ['members'], 'expected_rows': [[2]]},
    {'id': 'C12', 'question': 'which book has isbn ISBN-1', 'answer_tables': ['books'], 'expected_rows': [['Dune']]},
    {'id': 'C13', 'question': 'List book isbn and title', 'answer_tables': ['books'], 'expected_rows': [['ISBN-1', 'Dune'], ['ISBN-2', 'Emma'], ['ISBN-3', 'Kindred']]},
    {'id': 'C14', 'question': 'loan fee by status', 'answer_tables': ['loans'], 'expected_rows': [['open', 3.5], ['returned', 1.0]]},
    {'id': 'C15', 'question': 'sum of open loan fee', 'answer_tables': ['loans'], 'expected_rows': [[3.5]]},
    {'id': 'C16', 'question': 'How many returned loans?', 'answer_tables': ['loans'], 'expected_rows': [[1]]},
    {'id': 'C17', 'question': 'Show copies', 'answer_tables': ['copies'], 'expected_rows': [['shelf']]},
    {'id': 'C18', 'question': 'Show the book caption', 'answer_tables': ['books'], 'expected_rows': [['Dune'], ['Emma'], ['Kindred']], 'tags': ['ambiguous']},
    {'id': 'C19', 'question': 'What is the weather tomorrow?', 'answer_tables': [], 'expected_rows': None, 'tags': ['no-match']},
    {'id': 'C20', 'question': 'Where is the endowment stored?', 'answer_tables': [], 'expected_rows': None, 'tags': ['no-match']},
    {'id': 'C21', 'question': 'city for each loan', 'answer_tables': ['members', 'loans'], 'expected_rows': [['Kent'], ['Kent'], ['York']]},
    {'id': 'C22', 'question': 'How many books were written in 1815?', 'answer_tables': ['books'], 'expected_rows': [[1]]},
    {'id': 'C23', 'question': 'titles of books published before 1900', 'answer_tables': ['books'], 'expected_rows': [['Emma']]},
    {'id': 'C24', 'question': 'How many loans does Ann have?', 'answer_tables': ['members', 'loans'], 'expected_rows': [[2]]},
    {'id': 'C25', 'question': 'highest loan fee', 'answer_tables': ['loans'], 'expected_rows': [[2.5]]},
    {'id': 'C26', 'question': 'members with no loans', 'answer_tables': ['members', 'loans'], 'expected_rows': [['Cal']]},
    {'id': 'C27', 'question': 'books never loaned', 'answer_tables': ['books', 'loans'], 'expected_rows': [['Kindred']]},
    {'id': 'C28', 'question': 'average loan fee', 'answer_tables': ['loans'], 'expected_rows': [[1.5]]},
    {'id': 'C29', 'question': 'count loans per member name', 'answer_tables': ['members', 'loans'], 'expected_rows': [['Ann', 2], ['Bea', 1]]},
    {'id': 'C30', 'question': 'open loan titles', 'answer_tables': ['books', 'loans'], 'expected_rows': [['Dune'], ['Emma']]},
    {'id': 'C31', 'question': 'returned book title', 'answer_tables': ['books', 'loans'], 'expected_rows': [['Dune']]},
    {'id': 'C32', 'question': 'How many distinct cities?', 'answer_tables': ['members'], 'expected_rows': [[2]]},
    {'id': 'C33', 'question': 'isbn values sorted', 'answer_tables': ['books'], 'expected_rows': [['ISBN-1'], ['ISBN-2'], ['ISBN-3']], 'ordered': True},
    {'id': 'C34', 'question': 'loans with a null return note', 'answer_tables': ['loans'], 'expected_rows': [[2]]},
    {'id': 'C35', 'question': 'Show shelf copies', 'answer_tables': ['copies'], 'expected_rows': [['shelf']]},
    {'id': 'C36', 'question': 'How many active loans?', 'answer_tables': ['loans'], 'expected_rows': [[2]], 'tags': ['conflict']},
]


def confirmation_database(path):
    from contextlib import closing
    import sqlite3
    from pathlib import Path
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as conn:
        conn.executescript(
            'CREATE TABLE members(id INTEGER PRIMARY KEY, name TEXT, city TEXT);'
            'CREATE TABLE books(id INTEGER PRIMARY KEY, isbn TEXT, title TEXT, published INTEGER);'
            'CREATE TABLE loans(id INTEGER PRIMARY KEY, member_id INTEGER, book_id INTEGER, status TEXT, fee REAL, return_note TEXT,'
            ' FOREIGN KEY(member_id) REFERENCES members(id), FOREIGN KEY(book_id) REFERENCES books(id));'
            'CREATE TABLE copies(id INTEGER PRIMARY KEY, label TEXT);'
            "INSERT INTO members VALUES (1, 'Ann', 'Kent'), (2, 'Bea', 'York'), (3, 'Cal', 'Kent');"
            "INSERT INTO books VALUES (1, 'ISBN-1', 'Dune', 1965), (2, 'ISBN-2', 'Emma', 1815), (3, 'ISBN-3', 'Kindred', 1979);"
            "INSERT INTO loans VALUES (1, 1, 1, 'open', 2.5, NULL), (2, 1, 2, 'open', 1.0, NULL), (3, 2, 1, 'returned', 1.0, 'shelf');"
            "INSERT INTO copies VALUES (1, 'shelf');")
        conn.commit()
    return path
