chrome.action.onClicked.addListener(tab => {
  // open() needs the original toolbar gesture. A newly opened panel reads its
  // window's active tab on load; a panel already open receives this message.
  chrome.sidePanel.open({windowId: tab.windowId}).then(() => {
    return chrome.runtime.sendMessage({type: "diagnose", windowId: tab.windowId, tabId: tab.id});
  }).catch(error => {
    if (!error.message.includes("Receiving end does not exist")) console.error(error.message);
  });
});
