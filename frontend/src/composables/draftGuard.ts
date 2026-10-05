let dirtyCheck: () => boolean = () => false
export function draftGuard(check: () => boolean) {
  dirtyCheck = check
  return () => {
    if (dirtyCheck === check) dirtyCheck = () => false
  }
}
export function confirmDiscardDraft() {
  return !dirtyCheck() || window.confirm('手稿有未保存改动。确定放弃并退出登录？')
}
