from __future__ import annotations


class Plain:
    def __init__(self, text: str = ""):
        self.text = text


class Image:
    def __init__(self, file: str = "", url: str = "", sub_type: str = ""):
        self.file = file
        self.url = url
        self.sub_type = sub_type


class At:
    def __init__(self, qq: str = "", name: str = ""):
        self.qq = qq
        self.name = name


class AtAll:
    def __init__(self):
        pass


class Reply:
    def __init__(self, id: str = ""):
        self.id = id


class Face:
    def __init__(self, id: str = ""):
        self.id = id


class Record:
    def __init__(self, file: str = "", url: str = ""):
        self.file = file
        self.url = url


class Video:
    def __init__(self, file: str = "", url: str = ""):
        self.file = file
        self.url = url


class File:
    def __init__(self, name: str = "", file: str = "", url: str = ""):
        self.name = name
        self.file = file
        self.url = url


class Forward:
    def __init__(self, id: str = ""):
        self.id = id


class Poke:
    def __init__(self, id: str = ""):
        self.id = id


class Unknown:
    def __init__(self, **kw):
        self.__dict__.update(kw)