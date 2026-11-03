from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, computed_field


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class DepartmentOut(ORMModel):
    id: int
    name: str


class ClassOut(ORMModel):
    id: int
    name: str
    grade: int | None
    letter: str


class PersonOut(ORMModel):
    id: int
    full_name: str
    short_name: str
    last_name: str
    first_name: str
    middle_name: str
    email: str | None
    is_student: bool
    is_employee: bool
    is_external: bool
    is_active: bool
    organization: str | None
    study_class: ClassOut | None
    departments: list[DepartmentOut]


class PersonRow(PersonOut):
    """Строка списка персон: учётная запись, если человек уже входил или ему выданы права."""

    user_id: int | None = None
    is_superuser: bool = False


class PersonPage(BaseModel):
    items: list[PersonRow]
    total: int


class UserOut(ORMModel):
    id: int
    email: str
    is_active: bool
    is_superuser: bool
    last_login_at: datetime | None
    person: PersonOut | None
    password_hash: str | None = Field(default=None, exclude=True)

    @computed_field
    @property
    def has_password(self) -> bool:
        return bool(self.password_hash)


class UserPage(BaseModel):
    items: list[UserOut]
    total: int


class UserUpdate(BaseModel):
    is_active: bool | None = None
    is_superuser: bool | None = None
    password: str | None = Field(default=None, min_length=8, max_length=200)
    full_name: str | None = Field(default=None, max_length=300)
    is_employee: bool | None = Field(default=None, description='Сотрудник гимназии')


class PersonAdmin(BaseModel):
    is_superuser: bool


class UserCreate(BaseModel):
    email: EmailStr
    full_name: str = Field(default='', max_length=300)
    password: str | None = Field(default=None, min_length=8, max_length=200)
    is_superuser: bool = False
    is_employee: bool = False


class SyncRunOut(ORMModel):
    id: int
    mode: str
    status: str
    stats: dict
    error: str | None
    started_at: datetime
    finished_at: datetime | None


class SyncRequest(BaseModel):
    mode: str = Field(default='incremental', pattern='^(full|incremental)$')
